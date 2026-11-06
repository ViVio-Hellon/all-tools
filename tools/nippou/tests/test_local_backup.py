"""日報入力データの控え ── LocalBackup (v4.12.0)

    日報入力データがローカル保存とのことですが指定パスにも保存できるように
    (ローカルと両方)ローカルになければパスを見る / フォルダ名は LocalBackup
    権限 が Administrator のユーザーは LocalBackup すべてのラインデータを
    記録を見る で参照でき編集もできる / 編集後共有保存もできその保存先は
    読み込んだデータのラインとする

【約束】
    ・保存・送れた印・紙を消す → **同じ取引で**写す待ちに入れ、応答のあとで写す
    ・<控えの置き場所>\\LocalBackup\\<ライン>\\日報入力データ.sqlite3(親が無ければ作らない)
    ・届かなければ待ちに残る(しばらくは試さない)。届いたら写る
    ・新しいほうが正: 控えが新しければ上書きせず手元へ戻す / 起動で控え→手元
    ・手元に無い直は控えから見る(この端末のライン。管理者は全ライン)
    ・Administrator は控えの直を呼び出して直せる。共有へは**その日報のライン**の表へ
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config  # noqa: E402
from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.services import local_backup as lb  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

DAY = "2026年10月1日"


def save(repo: NippouRepository, *, line="L-1", shift="1直", page=1, mai="10",
         worker="山田", day=DAY) -> tuple[str, str, str, int]:
    key = dict(report_date=day, line=line, shift=shift, page=page)
    repo.save(HeaderRecord(**key, worker=worker),
              [DetailRecord(**key, row_no=1, lot="N7131T0", siz="8.000×1528.0×3053.0",
                            kz="08", kh="00", sz="10", sh="00", hit="2", mai=mai, tut="1",
                            con=mai, wei="100", tim="120", others1="H176", others4="1P0001")])
    return (day, line, shift, page)


def mai_of(repo: NippouRepository, key) -> str:
    loaded = repo.load(*key)
    return "" if loaded is None else loaded[1][0].mai


class BackupServiceTests(unittest.TestCase):
    """2台のPC(ラインのPC A と、管理者のPC B)と、共有のフォルダ。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.share = self.tmp / "共有"
        self.share.mkdir()
        patcher = patch.object(type(config.SETTINGS), "local_backup_base",
                               new_callable=PropertyMock, return_value=self.share)
        patcher.start()
        self.addCleanup(patcher.stop)
        lb.reset()
        self.addCleanup(lb.reset)
        self.a = self.pc("a")
        self.b = self.pc("b")

    def pc(self, name: str) -> NippouRepository:
        conn = connect(self.tmp / f"{name}.sqlite3")
        self.addCleanup(conn.close)
        return NippouRepository(conn)

    def live_settings(self, **props) -> None:
        """**いま読み込まれている** `config.SETTINGS` にも同じ置き場所を差す。ほかの試験(`tests._web`)が
        モジュールを読み込み直したあとだと、この試験の先頭で読んだ `config` とは別物になり、
        中で読み込むサービス(`load_factor`・`day_csv`)は新しいほうを見る。"""
        import importlib

        live = importlib.import_module("nippou.config")
        values = {"local_backup_base": self.share, **props}
        for module in {id(config): config, id(live): live}.values():
            for name, value in values.items():
                patcher = patch.object(type(module.SETTINGS), name,
                                       new_callable=PropertyMock, return_value=value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def backup_file(self, line="L-1") -> Path:
        return self.share / "LocalBackup" / line / "日報入力データ.sqlite3"

    # -- 写す -------------------------------------------------------------
    def test_保存と同じ取引で待ちに入り_写すと消える(self) -> None:
        key = save(self.a)
        self.assertEqual(self.a.backup_pending(), [key])
        self.assertTrue(self.a.backup_queued)
        result = lb.flush(self.a)
        self.assertEqual((result.written, result.pending, result.error), (1, 0, ""))
        self.assertTrue(self.backup_file().is_file())
        with lb.reader("L-1") as backup:
            self.assertEqual(mai_of(backup, key), "10")
            self.assertEqual(backup.load(*key)[0].worker, "山田")

    def test_写している最中に入れ直したページは待ちに残る(self) -> None:
        """控えへ写している最中に、同じページが**送り済みになった**(共有へ保存)とき。

        写し終えたときに「写した分」として待ちを消すと、送り済みにしたときに入れ直した
        待ちまで消え、控えは「未送信」のまま残っていた(管理者の「記録を見る」に未送信と出る)。
        """
        key = save(self.a)
        copy = lb._copy_page

        def copy_then_push(src, dst, page_key):
            done = copy(src, dst, page_key)
            if src is self.a.conn:                # 写した直後に、共有へ保存して送り済みにする
                self.a.mark_synced(key)
            return done

        with patch.object(lb, "_copy_page", side_effect=copy_then_push):
            lb.flush(self.a)
        self.assertEqual(self.a.backup_pending(), [key])      # 入れ直した待ちは残る
        lb.flush(self.a)
        self.assertEqual(self.a.backup_pending(), [])
        with lb.reader("L-1") as backup:
            dirty = backup.conn.execute(
                f"SELECT dirty FROM {lb.HEADER} WHERE report_date=? AND line=? AND shift=? AND page=?",
                key).fetchone()[0]
        self.assertEqual(dirty, 0)                              # 控えも送り済み

    def test_時計が進まなくても入れ直しを見分ける(self) -> None:
        """時刻を止めた(試験・時計の刻みが粗い)ときも、入れ直した待ちを消さない。

        待ちの印を時刻だけで作っていたので、保存と送り済みが同じ時刻だと同じ印になり、
        写し終えたときに入れ直した待ちまで消えていた(混在起動の試験でときどき落ちた)。
        """
        from datetime import datetime as real
        from nippou.db import repository as repo_mod

        class Frozen(real):
            @classmethod
            def now(cls, tz=None):
                return real(2026, 10, 5, 14, 30, 0)

        with patch.object(repo_mod, "datetime", Frozen):
            key = save(self.a)
            copy = lb._copy_page

            def copy_then_push(src, dst, page_key):
                done = copy(src, dst, page_key)
                if src is self.a.conn:
                    self.a.mark_synced(key)
                return done

            with patch.object(lb, "_copy_page", side_effect=copy_then_push):
                lb.flush(self.a)
            self.assertEqual(self.a.backup_pending(), [key])

    def test_写している最中の保存は_写し終えるのを待って続けて写す(self) -> None:
        """2つ目の書き写しは、1つ目が終わるのを待ってから写す(待たずに戻らない)。

        1つ目の最中に共有へ保存して送り済みにすると、その待ちは残る(上の試験)。そのとき
        2つ目が「ほかで写しています」で戻ると、残った待ちを写す人がいなくなり、控えが
        「未送信」のまま・待ちも残ったままだった(Windows の混在起動の試験で落ちた)。
        """
        import threading

        key = save(self.a)
        copy = lb._copy_page

        def second_flush() -> None:
            # 送ったあとの要求の書き写し(2つ目)。接続はその処理の中で開く(sqlite3 の決まり)
            conn = connect(self.tmp / "a.sqlite3")
            try:
                started["result"] = lb.flush(NippouRepository(conn), wait=10)
            finally:
                conn.close()

        started: dict = {}

        def copy_then_push(src, dst, page_key):
            done = copy(src, dst, page_key)
            if src is self.a.conn and "thread" not in started:
                self.a.mark_synced(key)               # 写している最中に送り済みになる
                # 送ったあとの要求の書き写し(2つ目)。1つ目はまだ錠を持っている
                started["thread"] = threading.Thread(target=second_flush)
                started["thread"].start()
                time.sleep(0.3)                       # 2つ目が錠を待ち始める
            return done

        with patch.object(lb, "_copy_page", side_effect=copy_then_push):
            lb.flush(self.a)
        started["thread"].join(timeout=15)
        self.assertNotEqual(started["result"].error, lb.BUSY)
        self.assertEqual(self.a.backup_pending(), [])
        with lb.reader("L-1") as backup:
            dirty = backup.conn.execute(
                f"SELECT dirty FROM {lb.HEADER} WHERE report_date=? AND line=? AND shift=? AND page=?",
                key).fetchone()[0]
        self.assertEqual(dirty, 0)

    def test_ほかのラインを直す管理者のPCは_前の直を控えから読んで係数を数える(self) -> None:
        """v4.46.3: 管理者のPCには呼び出した直しかない。前の直を見ずに係数を数えると、前の直から
        続いたロットをもう一度数えて、ロット数・係数Lot数を書き換えていた。"""
        from nippou import work_context
        from nippou.services import load_factor

        self.live_settings()
        save(self.a, line="機側", shift="1直")
        save(self.a, line="機側", shift="2直")
        lb.flush(self.a)
        lb.import_shift(self.b, DAY, "機側", "2直")
        self.assertEqual(self.b.saved_pages(DAY, "機側", "1直"), [], "管理者のPCには呼び出した直だけ")
        with patch.object(work_context, "terminal_line_decided", return_value=True), \
                patch.object(work_context, "stored_terminal_line", return_value="L-1"):
            pages = load_factor.previous_pages(self.b, DAY, "機側", "2直")
            self.assertEqual([[d.lot for d in p] for p in pages], [["N7131T0"]],
                             "前の直(1直)を控えから読む")
            # 控えに届かなければ数え直さない(前の直が揃わないまま数えない)
            import importlib

            live_lb = importlib.import_module("nippou.services.local_backup")
            with patch.object(live_lb, "reader", side_effect=OSError("届きません")):
                with self.assertRaises(load_factor.BackupUnread):
                    load_factor.previous_pages(self.b, DAY, "機側", "2直")
                self.assertFalse(load_factor.recalculate(self.b, DAY, "機側", "2直").applied)

    def test_ラインごとのフォルダ(self) -> None:
        save(self.a, line="L-1")
        save(self.a, line="HVC")
        lb.flush(self.a)
        self.assertTrue(self.backup_file("L-1").is_file())
        self.assertTrue(self.backup_file("HVC").is_file())
        self.assertEqual(lb.lines_available(), ["L-1", "HVC"])

    def test_置き場所に届かなければ作らず待つ(self) -> None:
        self.share.rmdir()
        key = save(self.a)
        result = lb.flush(self.a)
        self.assertIn("届きません", result.error)
        self.assertEqual(self.a.backup_pending(), [key])
        self.assertFalse(self.share.exists())                    # 作らない
        self.share.mkdir()
        self.assertIn("届きません", lb.flush(self.a).error)        # しばらくは試さない
        self.assertEqual(lb.flush(self.a, force=True).written, 1)
        self.assertEqual(self.a.backup_pending(), [])

    def test_送れた印も写る(self) -> None:
        key = save(self.a)
        lb.flush(self.a)
        self.a.mark_synced(key)
        self.assertEqual(self.a.backup_pending(), [key])
        lb.flush(self.a)
        self.assertEqual(lb.list_shifts("L-1")[0]["synced"], True)

    def test_紙を消せば控えからも消える(self) -> None:
        key = save(self.a)
        lb.flush(self.a)
        self.a.delete_page(*key)
        result = lb.flush(self.a)
        self.assertEqual(result.removed, 1)
        with lb.reader("L-1") as backup:
            self.assertIsNone(backup.load(*key))

    # -- 新しいほうが正 ---------------------------------------------------
    def test_動いているあいだも管理者の直しを取り込み_集計CSVを書き直す(self) -> None:
        """v4.46.3: 前は起動のときだけ控えを見ていた。ラインの端末が動いたままだと、管理者が直した
        前日分も、その日の集計CSVも直す前のまま。打っているページは戻さない。"""
        from nippou import work_context
        from nippou.reporting import csv_export

        out = self.tmp / "出力先"
        self.live_settings(report_output_dir=out)
        key = save(self.a, mai="10")
        today = save(self.a, shift="2直", mai="5")
        lb.flush(self.a)
        lb.import_shift(self.b, DAY, "L-1", "1直")
        lb.import_shift(self.b, DAY, "L-1", "2直")
        time.sleep(1.1)
        self.b.save(*_edited(self.b, key, "12"))
        self.b.save(*_edited(self.b, today, "99"))       # 打っているページへの直し(戻さない)
        lb.flush(self.b)
        lb.note_editing(today)
        with patch.object(work_context, "terminal_line_decided", return_value=True), \
                patch.object(work_context, "stored_terminal_line", return_value="L-1"):
            result = lb.pull_if_due(self.a, "L-1")
            self.assertEqual(result.pulled, 1)
            self.assertIsNone(lb.pull_if_due(self.a, "L-1"), "5分のうちは見に行かない")
        self.assertEqual(mai_of(self.a, key), "12")
        self.assertEqual(mai_of(self.a, today), "5", "画面で打っているページは戻さない")
        paths = csv_export.daily_output_paths(out, DAY, "L-1")
        self.assertTrue(paths["aggregate"].exists(), "取り込んだ日の集計CSVを書き直す")

    def test_管理者が呼び出して直す_通しで穴がない(self) -> None:
        """管理者が呼び出して直す、を通しで(v4.46.3。頻繁にある操作なので穴があってはならない)。

            ラインのPC(機側)が前日1〜3直を打って共有へ送り、集計CSVを書いた
            → 管理者のPC(この端末のラインは L-1)が控えから2直を入れて直す・共有へ送る
            → ラインのPCが動いたまま取り込む
        確かめること: 共有・控え・両方のPCの中身が直したものになる/ラインのPCの集計CSVは**1〜3直
        ぜんぶ**で直したものになる/管理者のPCは機側の集計CSVを書かない/送り済みのまま(送り直し
        や「共有へ出ていません」にならない)/管理者のPCの時計が遅れていても取り込む
        """
        import sqlite3

        from nippou import work_context
        from nippou.access_bridge import pusher
        from nippou.reporting import csv_export
        from nippou.services import day_csv

        out = self.tmp / "出力先"
        shared = self.tmp / "共有DB" / "日報データ.sqlite3"
        shared.parent.mkdir()
        out2 = self.tmp / "2つ目"
        self.live_settings(report_output_dir=out, summary_csv_dirs=[out, out2])
        keys = {sh: save(self.a, line="機側", shift=sh, mai=m)
                for sh, m in (("1直", "11"), ("2直", "22"), ("3直", "33"))}

        def as_pc(line):
            return [patch.object(work_context, "terminal_line_decided", return_value=True),
                    patch.object(work_context, "stored_terminal_line", return_value=line)]

        def run_as(line, fn):
            patches = as_pc(line)
            for p in patches:
                p.start()
            try:
                return fn()
            finally:
                for p in patches:
                    p.stop()

        # ラインのPC: 共有へ送り、控えへ写し、その日の集計CSVを書く
        self.assertEqual(len(pusher.push_pending(self.a, shared).succeeded), 3)
        lb.flush(self.a)
        run_as("機側", lambda: day_csv.settle(self.a, {k[:3] for k in keys.values()}, factors=False))
        agg = csv_export.daily_output_paths(out, DAY, "機側")["aggregate"]
        before = agg.read_text(encoding="utf-8-sig")
        # 管理者のPC: 控えから2直を入れて直す。**時計が遅れている**(直した時刻がラインのPCより前)
        lb.import_shift(self.b, DAY, "機側", "2直")
        header, details = _edited(self.b, keys["2直"], "29")
        details[0].con = "29"                            # 枚数(集計の合計枚数のもと)も
        self.b.save(header, details)
        with self.b.conn:
            self.b.conn.execute("UPDATE daily_header SET saved_at='2000-01-01T00:00:00'"
                                " WHERE line='機側' AND shift='2直'")
        notes = run_as("L-1", lambda: day_csv.settle(self.b, {keys["2直"][:3]}, factors=False))
        self.assertTrue(any("この端末では書き直しません" in n for n in notes), notes)
        self.assertEqual(agg.read_text(encoding="utf-8-sig"), before,
                         "管理者のPCは機側の集計CSVを書かない(1直・3直が消えない)")
        self.assertEqual(len(pusher.push_pending(self.b, shared).succeeded), 1)
        self.b.queue_backup([keys["2直"]])
        lb.flush(self.b, force=True)
        # ラインのPC: 動いたまま取り込む
        result = run_as("機側", lambda: lb.pull_if_due(self.a, "機側"))
        self.assertEqual(result.pulled, 1)
        # 中身: 共有・控え・両方のPC
        self.assertEqual(mai_of(self.a, keys["2直"]), "29")
        self.assertEqual(mai_of(self.b, keys["2直"]), "29")
        with lb.reader("機側") as backup:
            self.assertEqual(mai_of(backup, keys["2直"]), "29")
        conn = sqlite3.connect(shared)
        try:
            rows = conn.execute('SELECT "直" FROM "T_日報明細_機側"').fetchall()
            self.assertEqual(sorted(r[0] for r in rows), ["1直", "2直", "3直"])
        finally:
            conn.close()
        # 送り済みのまま(ラインのPCが送り直す・赤い帯を出すことがない)
        self.assertEqual([h for h in self.a.pending_sync_headers() if h.line == "機側"], [])
        # 集計CSV: 1〜3直ぜんぶで、2直は直した数
        after = agg.read_text(encoding="utf-8-sig")
        self.assertNotEqual(after, before, "直したあとの中身で書き直す")
        detail = csv_export.daily_output_paths(out, DAY, "機側")["detail"].read_text(encoding="utf-8-sig")
        for shift in ("1直", "2直", "3直"):
            self.assertIn(shift, detail)
        self.assertIn("機側,2直,29,", after, "2直の合計枚数は直した数")
        self.assertIn("機側,1直,11,", after)
        self.assertIn("機側,3直,33,", after)
        # 2つ目の出力先も(管理者が先に共有へ送ったので、ラインのPCは送り直さない ── 取り込んだときに書く)
        agg2 = csv_export.daily_output_paths(out2, DAY, "機側")["aggregate"].read_text(encoding="utf-8-sig")
        self.assertIn("機側,2直,29,", agg2)
        self.assertIn("機側,1直,11,", agg2)

    def test_管理者が共有へ送り忘れても_ラインのPCが取り込んで送る(self) -> None:
        """直しは控え経由でラインのPCに入り、送っていない印のまま ── ラインのPCの共有へ保存で届く。"""
        import sqlite3

        from nippou import work_context
        from nippou.access_bridge import pusher

        shared = self.tmp / "共有DB" / "日報データ.sqlite3"
        shared.parent.mkdir()
        self.live_settings(report_output_dir=self.tmp / "出力先")
        key = save(self.a, line="機側", shift="2直", mai="22")
        pusher.push_pending(self.a, shared)
        lb.flush(self.a)
        lb.import_shift(self.b, DAY, "機側", "2直")
        time.sleep(1.1)
        header, details = _edited(self.b, key, "29")
        details[0].con = "29"
        self.b.save(header, details)
        self.b.queue_backup([key])
        lb.flush(self.b, force=True)                       # 共有へは送らない
        with patch.object(work_context, "terminal_line_decided", return_value=True), \
                patch.object(work_context, "stored_terminal_line", return_value="機側"):
            self.assertEqual(lb.pull_if_due(self.a, "機側").pulled, 1)
        self.assertEqual([(h.shift, h.page) for h in self.a.pending_sync_headers()], [("2直", 1)],
                         "送っていない直しとしてラインのPCに入る")
        self.assertEqual(len(pusher.push_pending(self.a, shared).succeeded), 1)
        conn = sqlite3.connect(shared)
        try:
            got = conn.execute('SELECT * FROM "T_日報明細_機側"').fetchall()
        finally:
            conn.close()
        self.assertTrue(any("29" in [str(v) for v in row] for row in got), got)

    def test_管理者が直したぶんを_古い待ちで上書きしない(self) -> None:
        key = save(self.a, mai="10")
        self.a.queue_backup([key])                   # 写せないまま残った待ち(古い中身)
        # B(管理者)が控えから入れて直した(Aの待ちより新しい)
        save(self.b, mai="10")
        lb.flush(self.b)
        time.sleep(1.1)
        self.b.save(*_edited(self.b, key, "12"))
        lb.flush(self.b)
        result = lb.flush(self.a, force=True)
        self.assertEqual((result.written, result.pulled), (0, 1))
        self.assertEqual(mai_of(self.a, key), "12")              # 手元へ戻った
        with lb.reader("L-1") as backup:
            self.assertEqual(mai_of(backup, key), "12")          # 控えはそのまま

    def test_画面で打っている最中のページは戻さない(self) -> None:
        """v4.24.0: 開いて打っているページの中身を、画面の知らないうちに入れ替えない。

        控えの「新しい」は各PCの時計で決まるので、時計の進んだPCの古い中身でも
        戻ってしまう。打っているあいだは戻さず待ちに残し、離れてから揃える。
        """
        key = save(self.a, mai="10")
        self.a.queue_backup([key])
        save(self.b, mai="10")
        lb.flush(self.b)
        time.sleep(1.1)
        self.b.save(*_edited(self.b, key, "12"))
        lb.flush(self.b)
        lb.note_editing(key)                         # A の画面がこのページを打っている
        result = lb.flush(self.a, force=True)
        self.assertEqual(result.pulled, 0, "打っている最中のページを入れ替えた")
        self.assertEqual(mai_of(self.a, key), "10")
        self.assertEqual(self.a.backup_pending(), [key], "待ちから落ちた(あとで揃わない)")
        # 離れたら(しばらく書かなければ)、新しいほうに揃う
        with patch.object(lb, "EDITING_HOLD_SEC", 0.0):
            self.assertEqual(lb.flush(self.a, force=True).pulled, 1)
        self.assertEqual(mai_of(self.a, key), "12")

    def test_起動のとき_控えにだけある直を戻し_手元にだけある直を写す(self) -> None:
        theirs = save(self.b, shift="2直")
        lb.flush(self.b)
        mine = save(self.a, shift="1直")
        self.a.clear_backup_pending([mine])          # 仕組みが入る前に打ったぶん
        result = lb.pull(self.a, "L-1")
        self.assertEqual(result.pulled, 1)
        self.assertEqual(mai_of(self.a, theirs), "10")
        self.assertIsNotNone(self.a.load_packing_report(DAY, "L-1", "2直"))   # 集計も作り直す
        self.assertEqual(self.a.backup_pending(), [mine])        # 写す待ちへ
        lb.flush(self.a)
        self.assertEqual({s["shift"] for s in lb.list_shifts("L-1")}, {"1直", "2直"})

    def test_起動のとき_控えのほうが新しければ戻す(self) -> None:
        key = save(self.a, mai="10")
        lb.flush(self.a)
        lb.import_shift(self.b, DAY, "L-1", "1直")
        time.sleep(1.1)
        self.b.save(*_edited(self.b, key, "15"))
        lb.flush(self.b)
        self.assertEqual(lb.pull(self.a, "L-1").pulled, 1)
        self.assertEqual(mai_of(self.a, key), "15")
        self.assertEqual(lb.pull(self.a, "L-1").pulled, 0)       # 揃ったら何もしない

    def test_起動のとき_ほかのPCが同じ中身を送っていれば_送った印だけ揃える(self) -> None:
        """A が落ちて、次の直の B が控えから受け取った A のページを共有へ送った。翌朝 A を起動すると、
        A の手元ではまだ未送信のままで、送り済みの直が赤い帯で次の直を止めていた
        (通しの試験 `test_flow_multi_terminal.B_PreviousShiftTests`)。"""
        key = save(self.a)
        lb.flush(self.a)
        self.assertEqual(lb.pull(self.b, "L-1").pulled, 1)      # B の起動: A のページを受け取る
        self.b.mark_synced(key)                                  # B が共有へ送った
        lb.flush(self.b)
        result = lb.pull(self.a, "L-1")
        self.assertEqual((result.pulled, result.adopted), (0, 1))
        self.assertEqual(self.a.pending_sync_headers(), [], "B が送ったページが A では未送信のまま")
        self.assertEqual(mai_of(self.a, key), "10")
        self.assertEqual(lb.pull(self.a, "L-1").adopted, 0)     # 揃ったら何もしない

    def test_起動のとき_送り済みでも中身が違えば_送った印を付けない(self) -> None:
        """控えの「共有へ済」が手元と違う中身なら、手元の未送信はそのまま(送ったのは別の中身)。"""
        key = save(self.b, mai="10")
        self.b.mark_synced(key)
        lb.flush(self.b)
        save(self.a, mai="11")
        self.a.clear_backup_pending([key])
        self.a.conn.execute("UPDATE daily_header SET saved_at='2000-01-01T00:00:00'")  # 古いことにする
        self.a.conn.commit()
        result = lb.pull(self.a, "L-1")
        self.assertEqual((result.pulled, result.adopted), (1, 0))  # 新しい控えが入る(今までどおり)
        a2 = self.pc("a2")
        save(a2, mai="12")                                        # 控えより新しい・違う中身
        self.assertEqual(lb.pull(a2, "L-1").adopted, 0)
        self.assertEqual(len(a2.pending_sync_headers()), 1)

    def test_控えの直を手元へ入れる_手元のほうが新しいページは触らない(self) -> None:
        p1 = save(self.a, page=1, mai="10")
        p2 = save(self.a, page=2, mai="20")
        lb.flush(self.a)
        time.sleep(1.1)
        save(self.b, page=2, mai="99")               # Bで先に直していた(新しい)
        result = lb.import_shift(self.b, DAY, "L-1", "1直")
        self.assertEqual(result.pulled, 1)
        self.assertEqual((mai_of(self.b, p1), mai_of(self.b, p2)), ("10", "99"))
        self.assertIn("控えがありません", lb.import_shift(self.b, DAY, "HVC", "1直").error)

    def test_控えの直を並べる(self) -> None:
        save(self.a, shift="1直", page=1, worker="山田 鈴木")
        save(self.a, shift="1直", page=2)
        save(self.a, shift="2直", day="2026年10月2日")
        lb.flush(self.a)
        shifts = lb.list_shifts("L-1")
        self.assertEqual([(s["report_date"], s["shift"]) for s in shifts],
                         [("2026年10月2日", "2直"), (DAY, "1直")])
        self.assertEqual(shifts[1]["pages"], [1, 2])
        self.assertEqual(shifts[1]["workers"], ["山田", "鈴木"])
        self.assertFalse(shifts[1]["synced"])
        self.assertEqual(lb.list_shifts("HVC"), [])


def _edited(repo: NippouRepository, key, mai: str):
    header, details = repo.load(*key)
    details[0].mai = mai
    return header, details


class BackupWebTests(WebTestCase):
    """画面から: 保存のあとに写す / 記録を見る / 管理者が直して共有へ。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})

    def administrator(self):
        from nippou.services import access_rights
        patcher = patch.object(access_rights, "is_administrator", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def backup_of(self, line: str, *keys_and_mai) -> None:
        """そのラインのPCが保存して控えへ写した、を作る(別のPC)。"""
        other = NippouRepository(connect(self.tmp / f"{line}-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        for shift, mai in keys_and_mai:
            save(other, line=line, shift=shift, mai=mai)
        lb.flush(other, force=True)

    def test_保存すると_応答のあとで控えへ写る(self) -> None:
        from tests.test_auto_export import payload

        res = self.post("/api/entry/save", payload())
        self.assertTrue(res.get_json()["saved"], res.get_json())
        self.assertEqual(len(self.repo().backup_pending()), 1)
        res.close()                                   # 応答を返し終わった
        self.assertEqual(self.repo().backup_pending(), [])
        self.assertEqual(len(lb.list_shifts("L-1")), 1)

    def test_一般の端末は控えの札が出ず_ほかのラインは読めない(self) -> None:
        self.backup_of("HVC", ("1直", "10"))
        page = self.get("/records").get_data(as_text=True)
        self.assertNotIn('id="backup-card"', page)
        self.assertEqual(self.get("/api/records/backup?line=HVC").status_code, 403)
        self.assertEqual(self.post("/api/records/backup/open", {
            "report_date": DAY, "line": "HVC", "shift": "1直"}).status_code, 403)
        # 手元に無い他ラインの紙も、控えからは出さない
        body = self.post("/api/print/load", {"report_date": DAY, "line": "HVC",
                                             "shift": "1直"}).get_json()
        self.assertFalse(body["found"])

    def test_手元に無ければ_この端末のラインの控えを見る(self) -> None:
        self.backup_of("L-1", ("2直", "33"))
        body = self.post("/api/print/load", {"report_date": DAY, "line": "L-1",
                                             "shift": "2直"}).get_json()
        self.assertTrue(body["found"], body)
        self.assertTrue(body["from_backup"])
        self.assertEqual(body["rows"][0]["mai"], "33")
        html = self.get(f"/report/nippou?report_date={DAY}&line=L-1&shift=2直&page=all")
        self.assertEqual(html.status_code, 200)
        self.assertIn("N7131T0", html.get_data(as_text=True))

    def test_Administratorは全ラインの控えを見て_呼び出して直し_そのラインへ送る(self) -> None:
        from nippou import work_context
        from nippou.access_bridge import pusher, sqlite_backend

        self.administrator()
        self.backup_of("HVC", ("1直", "10"))
        page = self.get("/records").get_data(as_text=True)
        self.assertIn('id="backup-card"', page)
        self.assertIn('<option value="HVC"', page)
        listed = self.get("/api/records/backup?line=HVC").get_json()
        self.assertEqual([(s["shift"], s["in_local"]) for s in listed["shifts"]],
                         [("1直", False)])

        body = self.post("/api/records/backup/open", {
            "report_date": DAY, "line": "HVC", "shift": "1直", "page": 1}).get_json()
        self.assertEqual(body.get("next"), "/", body)
        ctx = work_context.get_context()
        self.assertTrue(ctx.recall.active)
        self.assertEqual((ctx.line, ctx.terminal_line), ("HVC", "L-1"))
        self.assertEqual(mai_of(self.repo(), (DAY, "HVC", "1直", 1)), "10")

        # 日報入力で直して保存(管理者モードは開けていない)
        state = self.post("/api/entry/state", {}).get_json()
        self.assertFalse(state["read_only"], state.get("message"))
        rows = {"1": {"LOT": "N7131T0", "KZ": "08", "KH": "00", "SZ": "10", "SH": "00",
                      "CON": "12", "WEI": "100", "MAI": "12", "HIT": "2"}}
        res = self.post("/api/entry/save", {"rows": rows, "header": {"worker": "山田"},
                                            "checks": {}})
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))
        self.assertEqual(mai_of(self.repo(), (DAY, "HVC", "1直", 1)), "12")

        # 共有へ: 送り先はその日報のライン(HVC)の表
        target = self.share / "日報データ.sqlite3"
        summary = pusher.push_pending(self.repo(), target)
        self.assertEqual(summary.failed, [])
        self.assertTrue(sqlite_backend.table_exists(target, "T_日報ヘッダー_HVC"))
        self.assertFalse(sqlite_backend.table_exists(target, "T_日報ヘッダー_L-1"))
        # 直したぶんは控えにも写る(HVC のPCは次の起動で受け取る)
        lb.flush(self.repo(), force=True)
        with lb.reader("HVC") as backup:
            self.assertEqual(mai_of(backup, (DAY, "HVC", "1直", 1)), "12")

    def test_ラインを持たない管理者PCでも_呼び出した紙は直せる(self) -> None:
        from nippou import user_settings, work_context

        user_settings.save_many({config.KEY_TERMINAL_LINE: ""})
        work_context.reset()
        self.administrator()
        self.backup_of("HVC", ("1直", "10"))
        state = self.post("/api/entry/state", {}).get_json()
        self.assertTrue(state["needs_line"])                     # ふだんは伏せたまま
        self.post("/api/records/backup/open", {
            "report_date": DAY, "line": "HVC", "shift": "1直", "page": 1})
        state = self.post("/api/entry/state", {}).get_json()
        self.assertFalse(state["needs_line"])                    # 呼び出した紙はHVCのもの

    def test_設定に控えの様子が出る(self) -> None:
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="backup-status"', page)
        self.assertIn(str(self.share / "LocalBackup"), page)
        self.assertIn("届きます", page)
        self.assertIn("日報入力データの控えの置き場所", page)


if __name__ == "__main__":
    unittest.main()
