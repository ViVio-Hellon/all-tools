"""読めている普通のときは、計算を断らない(v4.44.17 の現場の声「日報入力: 重量計算しなくなっている」)

v4.44.17 で「読めないのを『無い』と取り違えない」直しを入れた(`db.connection.file_state` / `open_rw` /
`connect_readonly` / `importer.failed_to_read` / `factor_master.read_factors` / `load_factor.recalculate`)。
読めないときに**計算しない**へ倒した所が、**読めている普通のとき**まで計算しなくなっていないかを確かめる。

    1. 共有を開く URI(`file_uri`)の形。Windows の道(C:\\… ・ Z:\\… ・ \\\\サーバ\\共有\\…)から作る文字列。
       SQLite の決まり: `file:` の後ろが `//` なら次はサーバ名(空か localhost 以外は断る)。
       それ以外は `%HH` を戻すだけで、道はそのまま SQLite の VFS へ渡る(Windows の VFS は
       `\\\\` で始まる道を UNC として、`C:\\` をドライブとして開く)
    2. Access の伝送用ファイルに `用途名負荷係数算出` の表が無い(読めている)。文言に「見つかりません」が
       入るので「届かない」に分けられ、係数Lot数・ﾛｯﾄ数を一度も計算し直さなかった
    3. ロット番号から単重 → 重量。係数の表を読めるとき、係数処理ﾛｯﾄ数・係数Lot数が出る(画面まで通す)
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


def mod(name: str):
    """そのつど引き直す(Web版の試験が `nippou.*` を読み込み直すので、先頭で掴むと古くなる)。"""
    return importlib.import_module(name)


# ======================================================================
# 1. 共有を開く URI の形
# ======================================================================
class FileUriTests(unittest.TestCase):
    def uri(self, text: str, mode: str = "rw") -> str:
        return mod("nippou.db.connection").file_uri(text, mode)

    def test_ドライブの道はそのまま書く(self) -> None:
        self.assertEqual(self.uri(r"C:\Users\line1\AppData\Local\NippouTool\x.sqlite3"),
                         r"file:C:\Users\line1\AppData\Local\NippouTool\x.sqlite3?mode=rw")

    def test_割り当てたドライブも同じ(self) -> None:
        self.assertEqual(self.uri(r"Z:\日報管理\日報データ.sqlite3", "ro"),
                         r"file:Z:\日報管理\日報データ.sqlite3?mode=ro")

    def test_UNCの道は先頭がサーバ名と読まれない形(self) -> None:
        # `\\` は URI の区切りではないので、SQLite はサーバ名(authority)と読まない
        self.assertEqual(self.uri(r"\\nlmsrv03\Read\【New】仕掛\台帳\SIKALOT.sqlite3", "ro"),
                         r"file:\\nlmsrv03\Read\【New】仕掛\台帳\SIKALOT.sqlite3?mode=ro")

    def test_スラッシュのUNCは空のサーバ名を前に足す(self) -> None:
        # `file://nlmsrv03/…` だと「invalid uri authority: nlmsrv03」で断られる
        uri = self.uri("//nlmsrv03/Read/台帳/SIKALOT.sqlite3", "ro")
        self.assertEqual(uri, "file:////nlmsrv03/Read/台帳/SIKALOT.sqlite3?mode=ro")
        self.assertFalse(uri.startswith("file://nlmsrv03"))

    def test_URIで意味を持つ字は書き直す(self) -> None:
        self.assertEqual(self.uri(r"\\srv\共有\50%#1?.sqlite3"),
                         r"file:\\srv\共有\50%25%231%3f.sqlite3?mode=rw")

    def test_この端末で実際に開ける(self) -> None:
        """作った URI を SQLite が受け付けて、そのファイルを開く(この OS の道で)。"""
        connection = mod("nippou.db.connection")
        with tempfile.TemporaryDirectory() as tmp:
            name = "日報 データ#50%.sqlite3"
            path = Path(tmp) / name
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE t (v)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            conn.close()
            ro = connection.connect_readonly(path)
            try:
                self.assertEqual(ro.execute("SELECT v FROM t").fetchone()[0], 1)
            finally:
                ro.close()
            rw = connection.open_rw(path)
            try:
                self.assertEqual(rw.execute("SELECT v FROM t").fetchone()[0], 1)
            finally:
                rw.close()
            if os.name != "nt":
                # `//` で始まる道(POSIX では同じ場所)も、サーバ名と読まれずに開ける
                ro = sqlite3.connect(connection.file_uri("/" + str(path), "ro"), uri=True)
                try:
                    self.assertEqual(ro.execute("SELECT v FROM t").fetchone()[0], 1)
                finally:
                    ro.close()


# ======================================================================
# 2. Access の伝送用ファイルに係数の表が無い(読めている)
# ======================================================================
def _runner(err_number, err_desc):
    from nippou.access_bridge.runner import ScriptResult

    class Runner:
        calls = 0

        def run(self, script_text: str) -> ScriptResult:
            Runner.calls += 1
            return ScriptResult(success=False, err_number=err_number, err_desc=err_desc)

    return Runner()


#: ACE/Jet の「表が無い」(3078)。ADO の番号は DB_E_NOTABLE
MISSING = (-2147217865,
           "入力テーブルまたはクエリ '用途名負荷係数算出' が見つかりませんでした。"
           "存在していること、および名前が正しく入力されていることを確認してください。")
MISSING_EN = (-2147217865,
              "The Microsoft Access database engine cannot find the input table or query "
              "'用途名負荷係数算出'. Make sure it exists and that its name is spelled correctly.")


class AccessMissingTableTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.master = Path(tmp.name) / "伝送用ファイル.accdb"
        self.master.write_bytes(b"\x00\x01\x00\x00Standard ACE DB")   # あることだけ分かればよい

    def read(self, err_number, err_desc):
        factor_master = mod("nippou.access_bridge.factor_master")
        return factor_master.read_factors(self.master, _runner(err_number, err_desc))

    def test_表が無いのは読めないではない(self) -> None:
        self.assertEqual(self.read(*MISSING), ({}, ""))

    def test_英語の文言でも同じ(self) -> None:
        self.assertEqual(self.read(*MISSING_EN), ({}, ""))

    def test_番号が無くても文言で分かる(self) -> None:
        self.assertEqual(self.read(None, MISSING[1]), ({}, ""))

    def test_ロック中は読めない(self) -> None:
        importer = mod("nippou.access_bridge.importer")
        from unittest.mock import patch
        with patch.object(mod("nippou.access_bridge.runner").time, "sleep", lambda s: None):
            table, problem = self.read(3260, "テーブル '用途名負荷係数算出' は他のユーザーがロックしています")
        self.assertEqual(table, {})
        self.assertTrue(problem)
        self.assertTrue(importer.UNREAD_KINDS)

    def test_ファイルに届かないのは読めない(self) -> None:
        _, problem = self.read(-2147467259, "ネットワークパスが見つかりません")
        self.assertTrue(problem)

    def test_係数Lot数を計算し直す(self) -> None:
        """表の無い Access の伝送用ファイルでも、機側の係数Lot数は計算する(係数は1)。"""
        from unittest.mock import patch

        from nippou.db.connection import connect
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        tmp = Path(self.master.parent)
        conn = connect(tmp / "local.sqlite3")
        self.addCleanup(conn.close)
        ensure_schema(conn)
        repo = NippouRepository(conn)
        key = dict(report_date="2026年10月1日", line="機側", shift="1直", page=1)
        repo.save(HeaderRecord(**key, worker="山田"), [
            DetailRecord(**key, row_no=1, lot="N7131T0", others1="H283",
                         kz="08", kh="00", sz="09", sh="00", mai="1", tut="1")])
        service = mod("nippou.services.load_factor")
        factor_master = mod("nippou.access_bridge.factor_master")
        runner = _runner(*MISSING)
        real = factor_master.read_factors
        with patch.object(factor_master, "read_factors",
                          lambda path=None, r=None: real(self.master, runner)):
            applied = service.recalculate(repo, key["report_date"], "機側", "1直")
        self.assertTrue(applied.applied, applied.as_dict())
        header, rows = repo.load(key["report_date"], "機側", "1直", 1)
        self.assertEqual(rows[0].keisu, "1.0")
        self.assertEqual(header.coefficient_lot_count, "1.0")


# ======================================================================
# 3. 画面まで: ロット番号 → 単重 → 重量、係数の表を読めるとき係数Lot数
# ======================================================================
LOT_COLUMNS = ("ﾛｯﾄ番号", "ｵｰﾀﾞｰ番号", "製造材質", "製造調質", "製造板厚", "製造板幅",
               "製造板丈", "用途ｺｰﾄﾞ", "用途名", "BOX実績_枚本数", "梱包_合紙")
HIKI_COLUMNS = ("ﾛｯﾄ番号", "受注番号", "引当数量", "引当調整NO", "引当番号")
ODR_COLUMNS = ("受注番号", "VC_表", "VC_裏", "合紙", "製品単重", "包装仕様NO", "EX_輸出区分")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScreenTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self._table("SIKALOT.sqlite3", "仕掛", LOT_COLUMNS, [
            ("H5422S0", "AB2234", "52S", "R", 20.0, 1528.0, 3053.0, "H283", "ﾌﾗﾂﾄ", "9", "0")])
        self._table("SIKAHIKI.sqlite3", "仕掛", HIKI_COLUMNS,
                    [("H5422S0", "AB2234", 100, "", "60717001")])
        self._table("SIKAODR.sqlite3", "仕掛", ODR_COLUMNS,
                    [("AB2234", "VE-20NF", "", "1", 250.04314, "", "")])
        self._table("伝送用ファイル.sqlite3", "用途名負荷係数算出",
                    ("用途コード", "PENAを1とした場合の換算係数"), [("H283", 1.5)])
        self.post("/api/settings/paths", {"gw_reference_dir": str(self.ref), "password": "nisk"})

    def _table(self, name, table, columns, rows) -> None:
        conn = sqlite3.connect(str(self.ref / name))
        with conn:
            cols = ", ".join(f'"{c}"' for c in columns)
            conn.execute(f'CREATE TABLE "{table}" ({cols})')
            conn.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in columns)})',
                             rows)
        conn.close()

    def _use_line(self, line: str) -> None:
        # ラインを変えるのは管理者モードだけ(`test_load_factor.ScreenTests` と同じ)
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/entry/line", {"line": line})

    def test_ロット番号から単重が入り重量が出る(self) -> None:
        self._use_line("AIM")
        body = self.post("/api/entry/lot", {"rows": {"1": {"LOT": "H5422S0"}}, "row": 1}).get_json()
        self.assertEqual(body["lot"]["state"], "filled", body.get("lot"))
        row = body["rows"]["1"]
        self.assertEqual(row["UNI"], "250.04314")
        row.update({"MAI": "2", "TUT": "3"})
        body = self.post("/api/entry/state", {"rows": {"1": row}, "row": 1,
                                              "changed": "TUT"}).get_json()
        self.assertEqual(body["rows"]["1"]["CON"], "6")
        self.assertEqual(body["rows"]["1"]["WEI"], "1500.3")
        self.assertEqual(body["header"]["weight_kg"], "1500.3")

    def test_係数の表を読めれば係数Lot数が出る(self) -> None:
        self._use_line("機側")
        row = {"LOT": "H5422S0", "KZ": "08", "KH": "00", "SZ": "09", "SH": "00",
               "MAI": "1", "TUT": "1", "others1": "H283", "WEI": "100"}
        body = self.post("/api/entry/save", {"rows": {"1": row}}).get_json()
        self.assertTrue((body.get("load_factor") or {}).get("applied"), body.get("load_factor"))
        self.assertEqual(body["header"]["lot_count"], "1")
        self.assertEqual(body["header"]["coefficient_lot_count"], "1.5")


if __name__ == "__main__":
    unittest.main()
