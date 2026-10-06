"""マスタ管理 ── 直す先は取り込み元だけ

【何を見張るか】
1. **パスが無ければ直せない**(``no_source``)。書き先が決まっていないのに
   「保存しました」と言わない
2. **見るだけは通す。** 中身を確かめられることと、書き換えられることは別
3. 直せないときも**理由がその場に出る**。「読めません」だけでは直せない

取り込み元が sqlite3 になってから、**書き込みまで通しで試せる**ように
なった (``WriteTests``)。Access のころは ODBC ドライバが要り、この環境では
断り方しか確かめられなかった ── 「保存できたつもり」を生む一番危ない
経路が、試験の外にあった。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import _web
from calendar_app import config, master_admin, settings as user_settings


def _point_at(case: unittest.TestCase, folder: Path) -> None:
    user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, str(folder))
    case.addCleanup(user_settings.set_value, user_settings.KEY_MASTER_DB_DIR, "")
    user_settings.set_value(user_settings.KEY_DATA_DB_DIR, str(folder))
    case.addCleanup(user_settings.set_value, user_settings.KEY_DATA_DB_DIR, "")
    _web.reset_sync()
    case.addCleanup(_web.reset_sync)


class NoSourceTests(unittest.TestCase):
    """参照パスが無ければ直せない。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        user_settings.set_value(user_settings.KEY_MASTER_DB_DIR, "")
        user_settings.set_value(user_settings.KEY_DATA_DB_DIR, "")

    def test_直せないと分かる(self) -> None:
        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertFalse(body["editable"])
        self.assertEqual(body["reason"], "no_source")

    def test_理由がその場に出る(self) -> None:
        """「読めません」だけでは現場もこちらも直せない。"""
        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertIn("参照パス", body["why"])

    def test_保存を断る(self) -> None:
        res = self.client.post("/api/master/save",
                               json={"key": "10", "values": {"名前": "山田"}},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_追加を断る(self) -> None:
        res = self.client.post("/api/master/add",
                               json={"values": {"管理番号": "99", "名前": "新人"}},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_削除を断る(self) -> None:
        res = self.client.post("/api/master/delete", json={"key": "10"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "no_source")

    def test_断ったときも画面は返す(self) -> None:
        """直せなかった理由を読みながら、いまの中身を確かめられるように。"""
        res = self.client.post("/api/master/delete", json={"key": "10"},
                               headers=_web.auth())
        body = res.get_json()
        self.assertIn("columns", body)
        self.assertIn("rows", body)


class WrongFolderTests(unittest.TestCase):
    """パスはあるが、そこに班員名簿が無い。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_ファイルが無ければ断る(self) -> None:
        _point_at(self, Path(self.tmp.name))
        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertFalse(body["editable"])
        self.assertEqual(body["reason"], "no_source")

    def test_壊れたファイルは開けないと言う(self) -> None:
        folder = Path(self.tmp.name)
        (folder / config.SOURCE_FILE_MASTER).write_bytes(b"not a database")
        _point_at(self, folder)

        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertFalse(body["editable"])
        self.assertEqual(body["reason"], "no_source")
        self.assertIn("開けません", body["why"])

    def test_班員名簿の無い取り込み元は断る(self) -> None:
        """開けるかどうかと、直せるかどうかは別。"""
        folder = Path(self.tmp.name)
        _web.make_source(folder, config.SOURCE_FILE_MASTER,
                         _web.SOURCE_SCHEMA)   # 休み管理はあるが班員名簿が無い
        _point_at(self, folder)

        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertFalse(body["editable"])
        self.assertEqual(body["reason"], "no_source")
        self.assertIn(config.TABLE_MEMBER, body["why"])


class ShapeTests(unittest.TestCase):
    """画面が読む形。"""

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def test_直せる表の一覧が出る(self) -> None:
        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertEqual([t["table"] for t in body["tables"]],
                         [m.table for m in master_admin.MANAGED])

    def test_行を指す鍵が分かる(self) -> None:
        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertEqual(body["row_key"], master_admin.ROW_KEY)

    def test_直せない表は断る(self) -> None:
        res = self.client.post("/api/master/save",
                               json={"table": config.TABLE_DATA, "key": "1",
                                     "values": {"登録内容": "x"}},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_editable")

    def test_値の指定が無ければ400(self) -> None:
        res = self.client.post("/api/master/save", json={"key": "10"},
                               headers=_web.auth())
        self.assertEqual(res.status_code, 400)

    def test_トークンが要る(self) -> None:
        self.assertEqual(self.client.get("/api/master").status_code, 403)


class WriteTests(unittest.TestCase):
    """本物の取り込み元へ**実際に書く**。

    見張るのは2つ。

    1. 取り込み元が変わったこと(直す相手はここ1つ)
    2. **手元も追いついたこと** ── 書いたあとその表だけ取り込み直す。
       ここが抜けると「直したのに効かない」になる
    """

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.folder = _web.with_source(self, master=True)
        self.path = self.folder / config.SOURCE_FILE_MASTER
        self.write_member("10", "山田太郎")

    def write_member(self, key: str, name: str) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                f'INSERT OR REPLACE INTO "{config.TABLE_MEMBER}" '
                '("管理番号","苗字","班","名前","読み","担当ライン") '
                'VALUES (?,?,?,?,?,?)',
                (key, name[:2], "B", name, "ヨミ", "L-1"))
            conn.commit()
        finally:
            conn.close()

    def members(self) -> dict[str, str]:
        """取り込み元の中身(管理番号 → 名前)。"""
        return {r["管理番号"]: r["名前"]
                for r in _web.read_source(self.path, config.TABLE_MEMBER)}

    def local_members(self) -> dict[str, str]:
        """手元の写しの中身。"""
        rows = self.conn.execute(
            f'SELECT "管理番号","名前" FROM "{config.TABLE_MEMBER}"').fetchall()
        return {r[0]: r[1] for r in rows}

    def post(self, path: str, **body):
        return self.client.post(f"/api/master/{path}", json=body,
                                headers=_web.auth())

    def test_直せる状態だと分かる(self) -> None:
        body = _web.json_of(self.client.get("/api/master", headers=_web.auth()))
        self.assertTrue(body["editable"])
        self.assertEqual([r["名前"] for r in body["rows"]], ["山田太郎"])

    def test_保存すると取り込み元が変わる(self) -> None:
        res = self.post("save", key="10", values={"名前": "山田花子"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.members()["10"], "山田花子")

    def test_保存すると手元も追いつく(self) -> None:
        """直した結果がその場で画面に効くこと。"""
        self.post("save", key="10", values={"名前": "山田花子"})
        self.assertEqual(self.local_members()["10"], "山田花子")

    def test_鍵は変えられない(self) -> None:
        """変えられると、同じ人が2行に増える。"""
        self.post("save", key="10", values={"管理番号": "99", "名前": "山田花子"})
        self.assertEqual(sorted(self.members()), ["10"])

    def test_無い行の保存は断る(self) -> None:
        res = self.post("save", key="404", values={"名前": "誰か"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "no_row")

    def test_行を足せる(self) -> None:
        res = self.post("add", values={"管理番号": "20", "名前": "佐藤一郎",
                                       "班": "A", "担当ライン": "コイル"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.members()["20"], "佐藤一郎")
        self.assertEqual(self.local_members()["20"], "佐藤一郎")

    def test_同じ鍵は足せない(self) -> None:
        res = self.post("add", values={"管理番号": "10", "名前": "別人"})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "already")
        self.assertEqual(self.members()["10"], "山田太郎")

    def test_必須が空なら書かない(self) -> None:
        res = self.post("add", values={"管理番号": "30", "名前": "  "})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "bad_value")
        self.assertNotIn("30", self.members())

    def test_行を消せる(self) -> None:
        self.write_member("20", "佐藤一郎")
        res = self.post("delete", key="20")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(sorted(self.members()), ["10"])

    def test_消すのは指した1行だけ(self) -> None:
        """WHERE の取り違えで全員が消えないこと。"""
        self.write_member("20", "佐藤一郎")
        self.write_member("30", "鈴木次郎")
        self.post("delete", key="20")
        self.assertEqual(sorted(self.members()), ["10", "30"])

    def test_控えを取ってから書く(self) -> None:
        """共有の上の SQLite は最悪「破損」になりうるので、戻せる状態を作る。"""
        from calendar_app import app_config

        self.post("save", key="10", values={"名前": "山田花子"})
        backups = list(app_config.local_dir("backup").glob("*"))
        self.assertTrue(backups, "控えが1つも作られていません")


class RulesTests(unittest.TestCase):
    """``master_admin`` が持つ決めごと(HTTP を通さずに見る)。"""

    def test_休み管理はこの画面に出さない(self) -> None:
        """いま有効な登録は**カレンダーで見て、カレンダーで直す。**"""
        self.assertNotIn(config.TABLE_DATA, master_admin.BY_TABLE)

    def test_削除履歴は見られるが直せない(self) -> None:
        """消したものを後から探すことがある。**記録なので直さない。**"""
        self.assertIn(config.TABLE_DEL_HISTORY, master_admin.BY_TABLE)
        self.assertNotIn(config.TABLE_DEL_HISTORY, master_admin.EDITABLE)
        self.assertFalse(master_admin.is_removable(config.TABLE_DEL_HISTORY))

    def test_全部の列を直せるのは班員名簿だけ(self) -> None:
        """端末一覧も同じ画面に並ぶが、あちらで直せるのは予約の1列だけ。"""
        whole = [m.table for m in master_admin.MANAGED
                 if m.editable and not m.editable_columns]
        self.assertEqual(whole, [config.TABLE_MEMBER])

    def test_取り込み元に無い列は受け取らない(self) -> None:
        """押しても黙って捨てられるのを防ぐため、そもそも受け取らない。"""
        cleaned, error = master_admin._clean(
            config.TABLE_MEMBER,
            {"名前": "山田太郎", "存在しない列": "x"},
            ["管理番号", "苗字", "班", "名前", "読み", "担当ライン"],
            creating=False)
        self.assertIsNone(error)
        self.assertEqual(cleaned, {"名前": "山田太郎"})

    def test_鍵の列はあとから変えられない(self) -> None:
        """変えられると、同じ人が2行に増える。"""
        cleaned, error = master_admin._clean(
            config.TABLE_MEMBER, {"管理番号": "99", "名前": "山田"},
            ["管理番号", "名前"], creating=False)
        self.assertIsNone(error)
        self.assertNotIn("管理番号", cleaned)

    def test_作るときは鍵を受ける(self) -> None:
        cleaned, error = master_admin._clean(
            config.TABLE_MEMBER, {"管理番号": "99", "名前": "山田"},
            ["管理番号", "名前"], creating=True)
        self.assertIsNone(error)
        self.assertEqual(cleaned["管理番号"], "99")

    def test_必須が空なら断る(self) -> None:
        _cleaned, error = master_admin._clean(
            config.TABLE_MEMBER, {"管理番号": "99", "名前": "  "},
            ["管理番号", "名前"], creating=True)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, master_admin.REFUSE_BAD_VALUE)

    def test_必須を空で上書きさせない(self) -> None:
        _cleaned, error = master_admin._clean(
            config.TABLE_MEMBER, {"名前": ""},
            ["管理番号", "名前"], creating=False)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason, master_admin.REFUSE_BAD_VALUE)

    def test_列は取り込み元にあるものだけ(self) -> None:
        """3つとも既にある事実を読むだけ(定義 ∩ 取り込み元)。"""
        names = [c.name for c in master_admin.columns(
            config.TABLE_MEMBER, ["管理番号", "名前"])]
        self.assertEqual(names, ["管理番号", "名前"])


if __name__ == "__main__":
    unittest.main()


class DeleteHistoryViewTests(unittest.TestCase):
    """削除履歴 ── **後から探しに来る記録。**

    カレンダーから消したものは履歴に残りますが、これまで見る場所が
    ありませんでした。「あの休み、誰がいつ消したのか」を調べるには
    DBを開ける人を探すしかなく、そこまでして確かめる人は居ません。

    直せないのは、**記録だから**です。直せる記録は記録になりません。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.data = _web.make_source(self.folder, config.SOURCE_FILE_DATA,
                                     _web.SOURCE_SCHEMA)
        _web.make_source(self.folder, config.SOURCE_FILE_MASTER,
                         _web.MASTER_SCHEMA)
        _point_at(self, self.folder)
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()

    def add(self, when: str, who: str, name: str, line: str = "L-1") -> None:
        conn = sqlite3.connect(self.data)
        conn.execute(
            f'INSERT INTO "{config.TABLE_DEL_HISTORY}" '
            '("削除日時","削除実行者","対象日付","区分","登録内容",'
            '"識別コード","班","ライン") VALUES (?,?,?,?,?,?,?,?)',
            (when, who, "2026/10/05", config.KUBUN_REST, name, "10", "A", line))
        conn.commit()
        conn.close()

    def page(self, query: str = ""):
        res = self.client.get(
            f"/api/master?table={config.TABLE_DEL_HISTORY}&q={query}",
            headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def test_マスタ確認から見られる(self) -> None:
        self.add("2026/10/05 09:00:00", "yamada@PC1", "山田太郎")
        body = self.page()
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["登録内容"], "山田太郎")
        self.assertEqual(body["rows"][0]["削除実行者"], "yamada@PC1")

    def test_表の選択肢に並ぶ(self) -> None:
        names = [t["table"] for t in self.page()["tables"]]
        self.assertIn(config.TABLE_DEL_HISTORY, names)

    def test_新しい順に出る(self) -> None:
        """**直近を探しに来る。** 古い順だと、毎回いちばん下まで送る。"""
        self.add("2026/10/01 09:00:00", "a@PC1", "古いほう")
        self.add("2026/10/09 17:30:00", "b@PC2", "新しいほう")
        self.assertEqual([r["登録内容"] for r in self.page()["rows"]],
                         ["新しいほう", "古いほう"])

    def test_探せる(self) -> None:
        self.add("2026/10/01 09:00:00", "a@PC1", "山田太郎")
        self.add("2026/10/02 09:00:00", "b@PC2", "鈴木一郎", line="HVC")
        self.assertEqual([r["登録内容"] for r in self.page("鈴木")["rows"]],
                         ["鈴木一郎"])
        self.assertEqual([r["登録内容"] for r in self.page("HVC")["rows"]],
                         ["鈴木一郎"])

    def test_直せない(self) -> None:
        self.add("2026/10/05 09:00:00", "yamada@PC1", "山田太郎")
        body = self.page()
        self.assertFalse(body["editable"])
        self.assertFalse(body["removable"])
        self.assertEqual(body["reason"], "not_editable")
        self.assertIn("記録", body["why"])

    def test_直そうとしても断る(self) -> None:
        """画面に出していなくても、要求は直接投げられる。"""
        self.add("2026/10/05 09:00:00", "yamada@PC1", "山田太郎")
        for path in ("/api/master/save", "/api/master/delete"):
            res = self.client.post(
                path, json={"table": config.TABLE_DEL_HISTORY,
                            "key": "2026/10/05 09:00:00",
                            "values": {"登録内容": "書き換え"}},
                headers=_web.auth())
            self.assertEqual(res.status_code, 422, path)
            self.assertEqual(res.get_json()["error"]["code"], "not_editable")

    def test_1件も無くても理由が出る(self) -> None:
        body = self.page()
        self.assertEqual(body["rows"], [])
        self.assertTrue(body["why"])

    def test_日時の無い行も落とさない(self) -> None:
        """見るだけの表で行を捨てると、**見せないほうが困る。**"""
        self.add("", "yamada@PC1", "日時が空")
        self.assertEqual(len(self.page()["rows"]), 1)


class SortTests(unittest.TestCase):
    """列名を押して並べ替える(``GET /api/master?sort=&desc=``)。

    **並べるのはサーバ。** 画面に出すのは ROW_LIMIT 行までなので、
    画面の中だけで並べると、出ていない行を含めた正しい順にならない。
    """

    def setUp(self) -> None:
        _web.reset_sync()
        self.conn = _web.bind_db(self)
        self.client = _web.make_client()
        self.folder = _web.with_source(self, master=True)
        self.path = self.folder / config.SOURCE_FILE_MASTER
        conn = sqlite3.connect(self.path)
        conn.executemany(
            f'INSERT INTO "{config.TABLE_MEMBER}" '
            '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
            [("2", "鈴木", "B", "鈴木一郎", "スズキ", "コイル"),
             ("10", "安藤", "", "安藤二郎", "アンドウ", "HVC"),
             ("1", "佐藤", "A", "佐藤三郎", "サトウ", "L-1")])
        conn.commit()
        conn.close()

    def keys(self, query: str = "") -> list[str]:
        body = _web.json_of(self.client.get(f"/api/master?table={config.TABLE_MEMBER}"
                                            + query, headers=_web.auth()))
        return [r["管理番号"] for r in body["rows"]]

    def test_数は数として並べる(self) -> None:
        """1, 10, 2 の順にしない。"""
        self.assertEqual(self.keys("&sort=管理番号"), ["1", "2", "10"])
        self.assertEqual(self.keys("&sort=管理番号&desc=1"), ["10", "2", "1"])

    def test_文字の列でも並べる(self) -> None:
        self.assertEqual(self.keys("&sort=読み"), ["10", "1", "2"])   # アンドウ・サトウ・スズキ

    def test_空欄はどちらの向きでも最後(self) -> None:
        self.assertEqual(self.keys("&sort=班")[-1], "10")
        self.assertEqual(self.keys("&sort=班&desc=1")[-1], "10")

    def test_知らない列なら既定の並び(self) -> None:
        body = _web.json_of(self.client.get(
            f"/api/master?table={config.TABLE_MEMBER}&sort=無い列", headers=_web.auth()))
        self.assertEqual(body["sort"], "")

    def test_切る前に並べる(self) -> None:
        """表示する行数を超えても、全体の中での順になる。"""
        from unittest import mock

        with mock.patch.object(master_admin, "ROW_LIMIT", 1):
            self.assertEqual(self.keys("&sort=管理番号&desc=1"), ["10"])

    def test_直したあとも同じ並びで返る(self) -> None:
        """既定の並びへ戻ると、いま直した行がどこへ行ったか探すことになる。"""
        res = self.client.post("/api/master/save", json={
            "table": config.TABLE_MEMBER, "key": "2", "values": {"名前": "鈴木一"},
            "view": {"sort": "管理番号", "desc": True}}, headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual([r["管理番号"] for r in body["rows"]], ["10", "2", "1"])
        self.assertEqual((body["sort"], body["desc"]), ("管理番号", True))
