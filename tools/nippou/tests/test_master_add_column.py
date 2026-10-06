"""マスタ管理で列を足す (v4.14.0 ── python-web-tools VER4.2.0 と同じ作り)

    マスタ管理で表を選び、「+ 列を足す…」を押します。
    入れるもの: 列の名前、型(文字 / 整数 / 小数)、最初の値(いまある行すべてに入ります。空なら空のまま)
    押すと、確認が1回出てから足します。管理者認証が要ります。
    足した列はすぐ一覧に出て、行を開けばほかの列と同じように直せます。共有の元ファイルに足すので、
    ほかの端末にも出ます。
    断る名前: もうある名前(英字の大文字・小文字だけ違うものも)、[ ] " . などの記号を含む名前、41文字以上の名前
    列を足せるのは、マスタ管理で直せる表だけ

【約束】
    ・鍵(管理者パスワード)が開いていなければ足さない(403)
    ・直せない表(上流のファイル・日報データ・見るだけの表)には足さない。VC計算マスタも
      (VC長さ計算の画面が列を決めて行を書く ── 足しても誰も埋めない)
    ・列を足す・最初の値を入れる・記録する、を1回で確定(途中で落ちたら列も足されない)
    ・足した列の記録(`ツールで足した列`)は同じファイルの中。マスタ管理の一覧には出さない
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

TABLE = "班員名簿"


class NameTests(unittest.TestCase):
    """名前と最初の値の確かめ(サーバが決める)。"""

    def setUp(self) -> None:
        from nippou import master_admin

        self.m = master_admin

    def test_断る名前(self) -> None:
        have = ["氏名", "班", "Code"]
        for name, needle in (("", "入れてください"),
                             ("氏名", "「氏名」という列がもうあります"),
                             ("code", "「Code」という列がもうあります"),   # 大文字・小文字だけ違う
                             ("CODE", "「Code」という列がもうあります"),
                             ("あ" * 41, "40文字まで"),
                             ("rowid", "中で使う名前"), ("__行", "中で使う名前")):
            with self.subTest(name=name):
                self.assertIn(needle, self.m.column_name_why(name, have))
        for char in ('[', ']', '"', '.', '`', "'", "\n"):
            with self.subTest(char=repr(char)):
                self.assertIn("は使えません", self.m.column_name_why(f"電話{char}番号", have))

    def test_通る名前(self) -> None:
        for name in ("電話番号", "あ" * 40, "PHONE_2", "メモ(作業長)", "No1"):
            with self.subTest(name=name):
                self.assertEqual(self.m.column_name_why(name, ["氏名"]), "")

    def test_最初の値は型に合わせる(self) -> None:
        iv = self.m.initial_value
        self.assertEqual(iv("text", "  未登録 "), ("未登録", ""))
        self.assertEqual(iv("text", ""), (None, ""))                   # 空なら空のまま
        self.assertEqual(iv("int", "３"), (3, ""))                      # 全角の数字
        self.assertEqual(iv("int", "1,000"), (1000, ""))
        self.assertEqual(iv("int", "2.0"), (2, ""))
        self.assertIn("小数は入れられません", iv("int", "1.5")[1])
        self.assertEqual(iv("real", "1.5"), (1.5, ""))
        self.assertIn("小数で入れて", iv("real", "あ")[1])
        self.assertIn("整数で入れて", iv("int", "nan")[1])

    def test_足せる表は直せる表だけ(self) -> None:
        self.assertEqual(self.m.add_column_why("material", TABLE), "")
        self.assertEqual(self.m.add_column_why("transmission", "時間用"), "")
        self.assertIn("マスタ管理で直す表にだけ", self.m.add_column_why("lot", "SIKALOT"))
        self.assertIn("マスタ管理で直す表にだけ", self.m.add_column_why("access_db", "T_日報ヘッダー_機側"))
        self.assertIn("マスタ管理で直す表にだけ", self.m.add_column_why("vc", "変更履歴"))
        self.assertIn("VC長さ計算の画面も行を書く", self.m.add_column_why("vc", "VC品種"))


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AddColumnTests(WebTestCase):
    """梱包資材マスタの班員名簿に列を足す。"""

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self.path = self.ref / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(self.path))
        with conn:
            conn.execute(f'CREATE TABLE "{TABLE}" ("氏名" TEXT, "班" TEXT, "Code" INTEGER)')
            conn.executemany(f'INSERT INTO "{TABLE}" VALUES (?,?,?)',
                             [("青木", "A", 1), ("木村", "B", 2), ("佐藤", "A", 3)])
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(self.ref), "password": "nisk"})

    # -- 道具 --------------------------------------------------------
    def unlock(self) -> None:
        self.post("/api/master/unlock", {"enable": True, "password": "nisk"})

    def add(self, name: str, kind: str = "text", initial: str = "", table: str = TABLE,
            file: str = "material"):
        return self.post("/api/master/column/add",
                         {"file": file, "table": table, "name": name, "kind": kind,
                          "initial": initial})

    def names(self, table: str = TABLE) -> list[str]:
        conn = sqlite3.connect(str(self.path))
        try:
            return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        finally:
            conn.close()

    def values(self, column: str) -> list[tuple]:
        conn = sqlite3.connect(str(self.path))
        try:
            return list(conn.execute(f'SELECT "{column}", typeof("{column}") FROM "{TABLE}"'
                                     " ORDER BY rowid"))
        finally:
            conn.close()

    def browse(self) -> dict:
        return self.get(f"/api/master/browse?file=material&table={TABLE}").get_json()

    # -- 試験 --------------------------------------------------------
    def test_鍵が要る(self) -> None:
        res = self.add("電話番号")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "locked")
        self.assertNotIn("電話番号", self.names())
        self.assertFalse(self.browse()["page"]["can_add_column"])

    def test_足すと一覧に出て_いまある行に最初の値が入る(self) -> None:
        self.unlock()
        before = self.browse()
        self.assertTrue(before["page"]["can_add_column"])
        self.assertIn("列は消せません", before["page"]["add_column_note"])
        res = self.add("電話番号", "text", "未登録")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertIn("班員名簿 に列「電話番号」(文字)を足しました。いまある 3行 に「未登録」を入れました",
                      body["message"])
        # すぐ一覧に出る(返ってきた状態に、もう入っている)
        self.assertIn("電話番号", body["page"]["columns"])
        self.assertEqual([r["電話番号"] for r in body["page"]["rows"]], ["未登録"] * 3)
        self.assertEqual(body["page"]["added_columns"], ["電話番号"])
        added = next(c for c in body["columns"] if c["name"] == "電話番号")
        self.assertTrue(added["added"])
        self.assertIn("計算・帳票には使われません", added["note"])
        # 共有の元ファイルに足す(ほかの端末にも出る)
        self.assertEqual(self.names(), ["氏名", "班", "Code", "電話番号"])
        # 記録は同じファイルの中。**一覧には出さない**
        conn = sqlite3.connect(str(self.path))
        try:
            record = conn.execute('SELECT "表", "列", "型", "最初の値" FROM "ツールで足した列"')
            self.assertEqual(list(record), [(TABLE, "電話番号", "text", "未登録")])
        finally:
            conn.close()
        group = next(g for g in body["catalog"] if g["file"] == "material")
        self.assertNotIn("ツールで足した列", [t["table"] for t in group["tables"]])
        hidden = self.get("/api/master/browse?file=material&table=ツールで足した列").get_json()
        self.assertIn("という表はありません", hidden["page"]["error"])

    def test_足した列は行を開いて直せる(self) -> None:
        self.unlock()
        self.add("電話番号")
        body = self.browse()
        key = body["page"]["rows"][0][body["row_key"]]
        res = self.post("/api/master/row/save",
                        {"file": "material", "table": TABLE, "key": key,
                         "values": {"電話番号": "090-1111-2222"}})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.values("電話番号")[0], ("090-1111-2222", "text"))
        self.assertEqual(self.values("電話番号")[1], (None, "null"))   # 最初の値が空なら空のまま

    def test_型どおりに入る(self) -> None:
        self.unlock()
        self.assertEqual(self.add("人数", "int", "３").status_code, 200)
        self.assertEqual(self.values("人数"), [(3, "integer")] * 3)
        self.assertEqual(self.add("係数", "real", "1.5").status_code, 200)
        self.assertEqual(self.values("係数"), [(1.5, "real")] * 3)
        body = self.browse()
        kinds = {c["name"]: c["kind_label"] for c in body["columns"]}
        self.assertEqual((kinds["人数"], kinds["係数"]), ("整数", "小数"))

    def test_断る名前は窓の中で言う(self) -> None:
        self.unlock()
        for name, needle in (("氏名", "「氏名」という列がもうあります"),
                             ("code", "「Code」という列がもうあります"),
                             ("電話[1]", "は使えません"), ('電"話', "は使えません"),
                             ("電.話", "は使えません"), ("あ" * 41, "40文字まで")):
            with self.subTest(name=name):
                res = self.add(name)
                self.assertEqual(res.status_code, 400)
                body = res.get_json()
                self.assertIn(needle, body["error"]["message"])
                self.assertEqual(body["page"]["table"], TABLE)        # 画面は最新の状態のまま
        self.assertEqual(self.add("あ" * 40).status_code, 200)         # 40文字までは足せる
        self.assertEqual(self.names(), ["氏名", "班", "Code", "あ" * 40])

    def test_型や最初の値が違えば足さない(self) -> None:
        self.unlock()
        self.assertEqual(self.add("人数", "int", "1.5").status_code, 400)
        self.assertEqual(self.add("人数", "date", "").status_code, 400)
        self.assertNotIn("人数", self.names())

    def test_途中で落ちたら列も足されない(self) -> None:
        """記録の表が書けない(形が違う)と、足した列ごと取り消す。"""
        conn = sqlite3.connect(str(self.path))
        with conn:
            conn.execute('CREATE TABLE "ツールで足した列" ("別の形" TEXT)')
        conn.close()
        self.unlock()
        res = self.add("電話番号", "text", "未登録")
        self.assertEqual(res.status_code, 422)
        self.assertIn("書けませんでした", res.get_json()["error"]["message"])
        self.assertNotIn("電話番号", self.names())

    def test_直せない表には足さない(self) -> None:
        self.unlock()
        res = self.add("メモ", file="lot", table="SIKALOT")
        self.assertEqual(res.status_code, 422)
        self.assertIn("マスタ管理で直す表にだけ列を足せます", res.get_json()["error"]["message"])
        res = self.add("メモ", file="vc", table="VC品種")
        self.assertEqual(res.status_code, 422)
        self.assertIn("VC長さ計算の画面も行を書く", res.get_json()["error"]["message"])
        res = self.add("メモ", table="無い表")
        self.assertEqual(res.status_code, 404)

    def test_画面にボタンと窓がある(self) -> None:
        page = self.get("/settings?tab=master").get_data(as_text=True)
        self.assertIn('id="tbl-add-col" hidden>+ 列を足す…</button>', page)
        self.assertIn('id="col-modal"', page)
        for needle in ('id="col-name" maxlength="40"', '<option value="int">整数</option>',
                       '<option value="real">小数</option>', "いまある行すべてに入ります。空なら空のまま"):
            self.assertIn(needle, page)
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "views"
              / "master.js").read_text(encoding="utf-8")
        self.assertIn('"/api/master/column/add"', js)
        self.assertIn("足した列は消せません。よろしいですか?", js)   # 確認は1回


if __name__ == "__main__":
    unittest.main()
