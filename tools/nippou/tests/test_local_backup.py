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
