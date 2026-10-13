"""「ロットを探す」の探す範囲 ── 共有の日報データ + この端末

    ロットを探す 探す範囲: 保存した日報の行です(打ちかけの自動保存も含みます)。…
    ローカル保存した範囲だけですか?
    んーLOT検索用にDB作ってもいいぐらいだと思ったんですが....

【約束】
    ・探す元は**共有の日報データ**(全 PC が「共有へ保存」で送った行)と、**この端末**
      (まだ送っていない行・打ちかけ)。同じページは手元を採る(ページの単位で)
    ・各行に「どこから」(共有 / この端末・未送信 …)
    ・共有は**読むだけ**(書かない・無ければ作らない)。時間と件数に上限
    ・共有が Access・まだ無い・届かない・時間切れ・読めない、なら探すのはやめず、
      手元(と控え)で探してそのわけを出す
    ・VBA の前の名前の表(`T_日報ヘッダー_LS`)も読む。同じページは正規の表を採る
    ・ラインは誰でも選べる(「全ライン」も)。呼び出すのは今の決まり(`ctx.editor`)のまま、
      行き先は行ごと(この端末のラインで手元にあるページは手元、ほかは控えから)
    ・共有にしか無い直も、紙と下見が開ける(共有から読む)
"""
from __future__ import annotations

import csv
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config  # noqa: E402
from nippou.access_bridge import sqlite_backend  # noqa: E402
from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.presenters import agg_admin, lot_search  # noqa: E402
from nippou.services import shared_reports  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402
from tests.test_lot_search import DAY, SPAN, save  # noqa: E402


def send(path: Path, *, day: str = DAY, line: str = "HVC", shift: str = "1直", page: int = 1,
         worker: str = "鈴木", lots=("N7131T0",)) -> None:
    """そのラインの PC が「共有へ保存」で送った、を作る(**本物の書き手で**)。"""
    from nippou.access_bridge import pusher

    key = dict(report_date=day, line=line, shift=shift, page=page)
    rows = [DetailRecord(**key, row_no=n, lot=lot, zai="A1100-O", siz="1.0×1000×2000",
                         ken="5", kz="13", kh="00", sz="14", sh="15", con="5", wei="50",
                         others2="一般", others3="△△商事")
            for n, lot in enumerate(lots, 1)]
    result = sqlite_backend.push_records(
        path, HeaderRecord(**key, worker=worker), rows,
        pusher.header_table_name(line), pusher.detail_table_name(line))
    assert result.success, result.error


def send_vba(path: Path, *, old: str = "LS", day: str = DAY, shift: str = "1直",
             page: object = "1", lots=("N7131T0",), worker: str = "VBA太郎") -> None:
    """VBA が前の名前の表へ書いた、を作る(ページ・行番号は字で入っている)。"""
    header_table, detail_table = f"T_日報ヘッダー_{old}", f"T_日報明細_{old}"
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            sqlite_backend.ensure_tables(conn, header_table, detail_table)
            conn.execute(f'INSERT INTO "{header_table}" ("報告日","ライン","直","ページ","担当者")'
                         " VALUES (?,?,?,?,?)", (day, old, shift, page, worker))
            for n, lot in enumerate(lots, 1):
                conn.execute(f'INSERT INTO "{detail_table}" ("報告日","ライン","直","ページ",'
                             '"行番号","LOT") VALUES (?,?,?,?,?,?)',
                             (day, old, shift, page, str(n), lot))
    finally:
        conn.close()


class SharedSearchTests(unittest.TestCase):
    """探す処理(`presenters/lot_search.search`)に、一時フォルダの共有を渡す。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.conn = connect(self.tmp / "手元.sqlite3")
        self.addCleanup(self.conn.close)
        self.repo = NippouRepository(self.conn)
        (self.tmp / "共有").mkdir()
        self.shared = self.tmp / "共有" / "日報データ.sqlite3"

    def find(self, lot: str = "N71", *, line: str = "L-1", own_line: str = "L-1",
             editor: bool = False, shared: Path | None = None):
        query = lot_search.LotQuery.of({"lot": lot, **SPAN}, line)
        return lot_search.search(self.repo, query, own_line=own_line, editor=editor,
                                 shared_path=shared or self.shared)

    @staticmethod
    def rows(found) -> list[dict[str, str]]:
        return [dict(zip(found.table.columns, row)) for row in found.table.rows]

    # -- どこから -------------------------------------------------------
    def test_共有だけにある行が出る_どこからは共有(self) -> None:
        send(self.shared, line="HVC", lots=("N710001",))
        found = self.find(line="HVC")
        self.assertEqual(found.shared, "ok")
        self.assertEqual([h["lot"] for h in found.hits], ["N710001"])
        row = self.rows(found)[0]
        self.assertEqual(row["どこから"], "共有")
        self.assertEqual(row["ライン"], "HVC")
        self.assertEqual(row["作業者"], "鈴木")           # ページの見出しの作業者
        self.assertEqual(row["開始"], "13:00")
        self.assertIn("共有の日報データ", found.scope)
        self.assertEqual(found.notice, "")

    def test_手元だけ_未送信の行も出る(self) -> None:
        send(self.shared, line="L-1", shift="2直", lots=("N710002",))
        save(self.repo, line="L-1", shift="1直", lots=("N710001",), dirty=True)
        found = self.find()
        self.assertEqual([(h["lot"], h["source"]) for h in found.hits],
                         [("N710001", "この端末・未送信"), ("N710002", "共有")])

    def test_両方にあるページは手元を採る_ページの単位で(self) -> None:
        """共有は送ったときの中身。手元で直した(ロット№を打ち直した・行を消した)ほうを出す。"""
        send(self.shared, line="L-1", lots=("N710001", "N710009"))
        save(self.repo, line="L-1", lots=("N710002",), dirty=True)   # 同じページを直した
        found = self.find()
        self.assertEqual([(h["lot"], h["source"]) for h in found.hits],
                         [("N710002", "この端末・未送信")])
        # 古い中身(共有にだけ残っている行)は出ない
        self.assertEqual(self.find("N710009").hits, [])
        self.assertEqual(self.find("N710001").hits, [])

    def test_送り済みで共有にもあるページは共有(self) -> None:
        send(self.shared, line="L-1", lots=("N710001",))
        save(self.repo, line="L-1", lots=("N710001",), dirty=False)
        found = self.find()
        self.assertEqual([h["source"] for h in found.hits], ["共有"])
        self.assertEqual(found.total, 1)                       # 二重に出ない

    def test_ほかのラインは選んだラインだけ(self) -> None:
        send(self.shared, line="HVC", lots=("N710001",))
        send(self.shared, line="AIM", lots=("N710002",))
        self.assertEqual([h["lot"] for h in self.find(line="HVC").hits], ["N710001"])
        self.assertEqual([h["lot"] for h in self.find(line="L-1").hits], [])

    def test_全ライン(self) -> None:
        save(self.repo, line="L-1", shift="1直", lots=("N710001",))
        send(self.shared, line="HVC", shift="1直", lots=("N710002",))
        send(self.shared, line="機側", shift="1直", lots=("N710003",))
        send(self.shared, line="AIM", day="2026年9月30日", lots=("N710000",))
        found = self.find(line=lot_search.ALL_LINES)
        # 作業日 → 直 → ライン(定義の表の順)
        self.assertEqual([(h["line"], h["lot"]) for h in found.hits],
                         [("AIM", "N710000"), ("L-1", "N710001"), ("HVC", "N710002"),
                          ("機側", "N710003")])
        self.assertIn("4ライン", found.summary)
        self.assertTrue(found.as_dict()["all_lines"])
        self.assertEqual(found.as_dict()["line_label"], "全ライン")
        self.assertIn("全ライン", found.table.note)

    def test_VBAの前の名前の表も読む_同じページは正規の表(self) -> None:
        send_vba(self.shared, old="LS", shift="1直", lots=("N710001",))
        send_vba(self.shared, old="LS", shift="2直", lots=("N710009",))
        send(self.shared, line="機側", shift="2直", lots=("N710002",))  # コンバート後に直した
        found = self.find(line="機側")
        self.assertEqual([(h["line"], h["shift"], h["page"], h["lot"]) for h in found.hits],
                         [("機側", "1直", 1, "N710001"), ("機側", "2直", 1, "N710002")])
        self.assertEqual(self.rows(found)[0]["作業者"], "VBA太郎")

    def test_共有でも全角_小文字を揃えて頭を比べる(self) -> None:
        send(self.shared, line="HVC", lots=("Ｎ７１３１Ｔ０", "AN71000"))
        self.assertEqual([h["lot"] for h in self.find("n713", line="HVC").hits],
                         ["Ｎ７１３１Ｔ０"])

    def test_期間の外の行は読まない(self) -> None:
        send(self.shared, line="HVC", day="2026年8月31日", lots=("N710001",))
        send(self.shared, line="HVC", day="2026年11月1日", lots=("N710002",))
        self.assertEqual(self.find(line="HVC").hits, [])

    # -- 読むだけ ---------------------------------------------------------
    def test_共有は読むだけ_書かない(self) -> None:
        send(self.shared, line="HVC", lots=("N710001",))
        before = self.shared.read_bytes()
        self.find(line="HVC")
        self.find(line=lot_search.ALL_LINES)
        self.assertEqual(self.shared.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.shared.parent.iterdir()),
                         ["日報データ.sqlite3"])

    def test_共有が無ければ作らない(self) -> None:
        self.find()
        self.assertFalse(self.shared.exists())

    # -- 読めないとき(探すのはやめない) ----------------------------------
    def test_共有がまだ無い(self) -> None:
        save(self.repo, lots=("N710001",))
        found = self.find()
        self.assertEqual(found.shared, "missing")
        self.assertEqual([h["lot"] for h in found.hits], ["N710001"])
        self.assertIn("共有の日報データ(日報データ.sqlite3)がまだ無いので、"
                      "この端末の分だけ探しました", found.notice)
        self.assertIn("この端末に保存した行", found.scope)

    def test_共有に届かない(self) -> None:
        save(self.repo, lots=("N710001",))
        found = self.find(shared=self.tmp / "届かない" / "日報データ.sqlite3")
        self.assertEqual(found.shared, "unreachable")
        self.assertEqual(found.total, 1)
        self.assertIn("に届かないので、この端末の分だけ探しました", found.notice)

    def test_共有がAccess(self) -> None:
        save(self.repo, lots=("N710001",))
        accdb = self.tmp / "共有" / "日報データ.accdb"
        accdb.write_bytes(b"not really access")
        found = self.find(shared=accdb)
        self.assertEqual(found.shared, "access")
        self.assertEqual(found.total, 1)
        self.assertIn("が Access なので、この端末の分だけ探しました", found.notice)

    def test_共有が壊れていて読めない(self) -> None:
        save(self.repo, lots=("N710001",))
        self.shared.write_bytes(b"this is not sqlite" * 100)
        found = self.find()
        self.assertEqual(found.shared, "error")
        self.assertEqual(found.total, 1)
        self.assertIn("を読めなかったので、この端末の分だけ探しました", found.notice)
        self.assertIn("詳しく:", found.notice)

    def test_時間切れ_開くのが遅い(self) -> None:
        send(self.shared, line="L-1", lots=("N710009",))
        save(self.repo, lots=("N710001",))
        real = shared_reports._open

        def slow(path, deadline):
            time.sleep(1.0)
            return real(path, deadline)

        started = time.monotonic()
        with patch.object(shared_reports, "SECONDS", 0.2), \
                patch.object(shared_reports, "_open", slow):
            found = self.find()
        self.assertLess(time.monotonic() - started, 1.0)       # 待たせすぎない
        self.assertEqual(found.shared, "timeout")
        self.assertEqual([h["lot"] for h in found.hits], ["N710001"])
        self.assertIn("0.2秒を超えたので、この端末の分だけ探しました", found.notice)
        time.sleep(1.0)                                        # 置いていった糸を待つ

    def test_時間切れ_問い合わせが遅い(self) -> None:
        """期限を過ぎた問い合わせは止める(`set_progress_handler`)。"""
        send(self.shared, line="HVC", lots=tuple(f"N7{n:05d}" for n in range(40)))

        def slow_match(text: str) -> bool:
            time.sleep(0.02)
            return True

        readout = shared_reports.lot_rows(["HVC"], *(lot_search._date(SPAN[k], k)
                                                      for k in ("start", "end")),
                                          slow_match, path=self.shared, seconds=0.2)
        self.assertEqual(readout.status, shared_reports.TIMEOUT)
        self.assertEqual(readout.rows, [])

    def test_件数の上限(self) -> None:
        send(self.shared, line="HVC", lots=tuple(f"N71000{n}" for n in range(6)))
        with patch.object(shared_reports, "CAP", 3):
            found = self.find(line="HVC")
        self.assertEqual(found.total, 3)
        self.assertIn("3行以上", found.summary)
        self.assertIn("共有で当たった行が 3行を超えたので", found.warning)
        self.assertIn("絞ってください", found.warning)

    def test_画面の上限は共有と手元を合わせて(self) -> None:
        send(self.shared, line="L-1", shift="2直", lots=("N710003", "N710004"))
        save(self.repo, line="L-1", lots=("N710001", "N710002"))
        with patch.object(lot_search, "LIMIT", 3):
            found = self.find()
        self.assertEqual(found.total, 4)
        self.assertTrue(found.too_many)
        self.assertEqual([h["lot"] for h in found.hits], ["N710001", "N710002", "N710003"])

    # -- 呼び出す ---------------------------------------------------------
    def test_呼び出すは今の決まり_行き先は行ごと(self) -> None:
        save(self.repo, line="L-1", shift="1直", lots=("N710001",))
        send(self.shared, line="L-1", shift="2直", lots=("N710002",))   # 手元に無い
        send(self.shared, line="HVC", shift="1直", lots=("N710003",))
        found = self.find(line=lot_search.ALL_LINES, editor=True)
        self.assertTrue(found.recall_allowed)
        self.assertEqual({h["lot"]: h["recall_path"] for h in found.hits},
                         {"N710001": lot_search.RECALL_LOCAL,
                          "N710002": lot_search.RECALL_BACKUP,
                          "N710003": lot_search.RECALL_BACKUP})
        self.assertFalse(self.find(line=lot_search.ALL_LINES, editor=False).recall_allowed)

    # -- ラインの選び方 ---------------------------------------------------
    def test_ラインの選び方(self) -> None:
        self.assertEqual(lot_search.pick_line("", "L-1"), "L-1")
        self.assertEqual(lot_search.pick_line("HVC", "L-1"), "HVC")
        self.assertEqual(lot_search.pick_line("*", "L-1"), lot_search.ALL_LINES)
        with self.assertRaises(lot_search.BadRequest) as caught:
            lot_search.pick_line("コイル", "L-1")
        self.assertEqual(caught.exception.field, "line")
        screen = lot_search.screen(lot_search.date(2026, 10, 13), "L-1")
        self.assertEqual(screen["line"], "L-1")
        self.assertEqual([ln["code"] for ln in screen["lines"]][-1], lot_search.ALL_LINES)
        self.assertEqual([ln["code"] for ln in screen["lines"]][:3], ["L-1", "LVC", "HVC"])


class LotListCsvTests(unittest.TestCase):
    def test_集計管理のロット一覧にはロット番号の列がもうある(self) -> None:
        """「集計用CSVにLOTNOを出しておく」── ロット一覧(とそのCSV)の5列目に出ている。"""
        labels = [label for _, label, _ in agg_admin.LOT_COLUMNS]
        self.assertEqual(labels[4], "ロット№")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SharedSearchWebTests(WebTestCase):
    """経路: 共有を `設定の access_dir` に置いて、探す・紙・下見・CSV。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        from nippou.services import local_backup as lb
        self.lb = lb
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.out = self.tmp / "出力"
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share),
                                 config.KEY_REPORT_OUT_DIR: str(self.out)})
        lb.reset()
        self.addCleanup(lb.reset)
        self.shared = self.share / "日報データ.sqlite3"
        save(self.repo(), shift="1直", lots=("N7131T0",))
        send(self.shared, line="HVC", shift="2直", worker="鈴木", lots=("N719999",))

    def search(self, lot: str = "N71", **extra):
        return self.post("/api/records/lot-search", {**SPAN, "lot": lot, **extra})

    def key(self, hit: dict) -> dict:
        return {k: hit[k] for k in ("report_date", "line", "shift", "page")}

    def test_一般の端末でも共有からほかのラインを探せる(self) -> None:
        body = self.search(line="HVC").get_json()
        self.assertEqual(body["line"], "HVC")
        self.assertEqual(body["shared"], "ok")
        self.assertEqual([(h["lot"], h["source"]) for h in body["hits"]],
                         [("N719999", "共有")])
        self.assertFalse(body["recall"]["allowed"])          # 見るだけ

    def test_全ラインは手元と共有をまとめて(self) -> None:
        body = self.search(line="*").get_json()
        self.assertEqual(body["line_label"], "全ライン")
        self.assertEqual([(h["line"], h["lot"]) for h in body["hits"]],
                         [("L-1", "N7131T0"), ("HVC", "N719999")])
        self.assertIn("どこから", body["table"]["columns"])

    def test_共有にしか無い直の紙と下見が開ける(self) -> None:
        hit = self.search(line="HVC").get_json()["hits"][0]
        loaded = self.post("/api/print/load", self.key(hit)).get_json()
        self.assertTrue(loaded["found"], loaded)
        self.assertTrue(loaded["from_shared"])
        self.assertFalse(loaded["from_backup"])
        self.assertEqual(loaded["worker"], "鈴木")
        self.assertEqual(loaded["pages"], [1])
        self.assertEqual([r["lot"] for r in loaded["rows"]], ["N719999"])
        for page in ("1", "all"):
            with self.subTest(page=page):
                paper = self.get("/report/nippou", query_string={**self.key(hit), "page": page})
                self.assertEqual(paper.status_code, 200)
                self.assertIn("N719999", paper.get_data(as_text=True))
        # 共有を読んだだけで、手元には入らない
        self.assertEqual(self.repo().saved_pages(hit["report_date"], "HVC", "2直"), [])

    def test_共有が読めず紙が無いときはわけを言う(self) -> None:
        from nippou import user_settings

        user_settings.save(config.KEY_ACCESS_DB_FILE, "日報データ.accdb")
        self.shared.unlink()
        key = {"report_date": DAY, "line": "HVC", "shift": "2直", "page": 1}
        loaded = self.post("/api/print/load", key).get_json()
        self.assertFalse(loaded["found"])
        self.assertIn("Access なので", loaded["message"])
        paper = self.get("/report/nippou", query_string=key)
        self.assertEqual(paper.status_code, 404)
        self.assertIn("Access なので", paper.get_data(as_text=True))

    def test_共有がAccessなら手元と控えで探してそう言う(self) -> None:
        from nippou import user_settings
        from nippou.services import access_rights

        user_settings.save(config.KEY_ACCESS_DB_FILE, "日報データ.accdb")
        self.shared.unlink()
        # HVC の PC が控えへ写した(Administrator は控えを読める)
        other = NippouRepository(connect(self.tmp / "HVC-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        save(other, line="HVC", lots=("N718888",))
        self.lb.flush(other, force=True)
        with patch.object(access_rights, "is_administrator", return_value=True):
            body = self.search(line="*").get_json()
        self.assertEqual(body["shared"], "access")
        self.assertIn("が Access なので、この端末と控えの分だけ探しました", body["notice"])
        self.assertEqual([(h["line"], h["lot"], h["source"]) for h in body["hits"]],
                         [("L-1", "N7131T0", "この端末・未送信"),
                          ("HVC", "N718888", "控え・未送信")])
        # 一般の端末は控えを読まない(今の決まり)
        body = self.search(line="*").get_json()
        self.assertEqual([h["lot"] for h in body["hits"]], ["N7131T0"])
        self.assertIn("この端末の分だけ探しました", body["notice"])

    def test_全ラインのCSV(self) -> None:
        body = self.post("/api/records/lot-search/csv",
                         {**SPAN, "lot": "N71", "line": "*"}).get_json()
        out = Path(body["file"])
        self.assertEqual(out.name, "ロットを探す_N71_2026-09-01_2026-10-31_全ライン.csv")
        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][-1], "どこから")
        self.assertEqual([(r[1], r[5], r[-1]) for r in rows[1:]],
                         [("L-1", "N7131T0", "この端末・未送信"), ("HVC", "N719999", "共有")])

    def test_管理者は共有にしか無いほかのラインの行を控えから呼び出す(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.search(line="HVC").get_json()
        self.assertTrue(body["recall"]["allowed"])
        hit = body["hits"][0]
        self.assertEqual(hit["recall_path"], "/api/records/backup/open")
        # 控えに無ければ、押して何も起きないのではなく、わけが返る
        res = self.post(hit["recall_path"], self.key(hit))
        self.assertEqual(res.status_code, 404)
        self.assertIn("message", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
