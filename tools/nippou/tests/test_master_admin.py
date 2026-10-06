"""表を見る / 直す ── sqlite3 には開く道具が無い

【なぜ要るのか】
参照するマスタは起動のたびに黙って読まれるだけで、中に何が入っているかを
見る手立ても、間違いを直す手立ても画面にありませんでした。

しかも相手は **sqlite3** です。Access なら現場のPCで開いて直せましたが、
sqlite3 は**開くための道具が入っていない前提**で考えるほかありません
(テキストエディタでも開けないバイナリ形式です)。

ここが守るのは3つ:
    ・見るのは誰でもできること
    ・**直すのはパスワードを入れたときだけ**であること
    ・上流のファイル(仕掛3つ)と、自分が書く先(日報管理)は直せないこと
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase  # noqa: E402


class MasterEditTests(WebTestCase):
    """伝送用ファイル(現場が持つ表)を直す。"""

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self.path = self.ref / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(self.path))
        with conn:
            conn.execute('CREATE TABLE "作業停止時間内訳_1" '
                         '("管理番号" INTEGER, "内訳" TEXT, "内訳番号" TEXT, "備考" TEXT)')
            conn.executemany(
                'INSERT INTO "作業停止時間内訳_1" VALUES (?,?,?,?)',
                [(1, "休憩食事", "0", ""), (2, "TPM活動/清掃", "1", "")])
            conn.execute('CREATE TABLE "時間用" '
                         '("番号" INTEGER, "直" TEXT, "開始" TEXT, "終了" TEXT)')
            conn.execute('INSERT INTO "時間用" VALUES (1, "1", "08:00", "17:00")')
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(self.ref), "password": "nisk"})

    # -- 道具 --------------------------------------------------------
    def browse(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.get(f"/api/master/browse?{query}").get_json()

    def unlock(self, password: str = "nisk"):
        return self.post("/api/master/unlock",
                         {"enable": True, "password": password})

    def rows(self) -> list[tuple]:
        conn = sqlite3.connect(str(self.path))
        try:
            return list(conn.execute(
                'SELECT "内訳", "内訳番号" FROM "作業停止時間内訳_1" ORDER BY rowid'))
        finally:
            conn.close()

    # -- 見る --------------------------------------------------------
    def test_パスワード無しでも見られる(self) -> None:
        """**見えることと直せることは別の話。**"""
        body = self.browse(file="transmission", table="作業停止時間内訳_1")
        self.assertFalse(body["unlocked"])
        self.assertEqual(body["page"]["total"], 2)
        self.assertEqual(body["page"]["rows"][0]["内訳"], "休憩食事")

    def test_行を指す番号が付いてくる(self) -> None:
        """主キーの無い表もある。**同じ値の行が2つ**あっても1行だけ指せる。"""
        body = self.browse(file="transmission", table="作業停止時間内訳_1")
        key = body["row_key"]
        self.assertTrue(all(key in row for row in body["page"]["rows"]))

    def test_表の一覧に説明が付く(self) -> None:
        """名前から中身が読めない表だけ、短い説明を添える。"""
        body = self.browse(file="transmission")
        notes = {t["table"]: t["note"] for t in body["tables"]}
        self.assertIn("停止理由", notes["作業停止時間内訳_1"])
        self.assertIn("直の境界時刻", notes["時間用"])

    def test_どの列でも絞り込める(self) -> None:
        """どの列に何が入っているかを覚えていなくても引ける。"""
        body = self.browse(file="transmission", table="作業停止時間内訳_1",
                           q="TPM")
        self.assertEqual(len(body["page"]["rows"]), 1)
        self.assertEqual(body["page"]["rows"][0]["内訳"], "TPM活動/清掃")

    def test_見出しで並べ替えられる(self) -> None:
        body = self.browse(file="transmission", table="作業停止時間内訳_1",
                           sort="内訳番号", sort_dir="desc")
        self.assertEqual([r["内訳番号"] for r in body["page"]["rows"]], ["1", "0"])

    # ---- 列名を押して並び替える(v3.98.0) --------------------------
    #
    #     マスタ確認・管理で列名クリックでソートするようにしてください
    def sort_table(self, values: list) -> None:
        """数が**文字で**入っている列(Access から移したマスタによくある)。"""
        conn = sqlite3.connect(str(self.path))
        with conn:
            conn.execute('CREATE TABLE "並べる" ("名前" TEXT, "値" TEXT)')
            conn.executemany('INSERT INTO "並べる" VALUES (?, ?)',
                             [(f"行{i}", v) for i, v in enumerate(values)])
        conn.close()

    def order(self, sort: str = "値", sort_dir: str = "asc") -> list:
        body = self.browse(file="transmission", table="並べる", sort=sort, sort_dir=sort_dir)
        return [r["値"] for r in body["page"]["rows"]]

    def test_文字で入った数も数として並ぶ(self) -> None:
        """文字のまま並べると 1, 10, 2 … になり、押しても並んで見えない。"""
        self.sort_table(["10", "9", "", "abc", "2", None, "1.5", "-3", "100"])
        self.assertEqual(self.order(sort_dir="asc"),
                         ["-3", "1.5", "2", "9", "10", "100", "abc", "", ""])
        # 逆順でも空は最後(探している行が空の山に埋もれない)
        self.assertEqual(self.order(sort_dir="desc"),
                         ["100", "10", "9", "2", "1.5", "-3", "abc", "", ""])

    def test_並べないときは表の順(self) -> None:
        self.sort_table(["10", "9", "2"])
        self.assertEqual(self.order(sort=""), ["10", "9", "2"])
        body = self.browse(file="transmission", table="並べる", sort="", sort_dir="asc")
        self.assertEqual(body["page"]["sort"], "")

    def test_出しきれないときも全件で並べる(self) -> None:
        """上から200件を出すとき、**200件の中だけで並べない**(全件で並べてから切る)。"""
        self.sort_table([str(i) for i in range(250)])
        body = self.browse(file="transmission", table="並べる", sort="値", sort_dir="desc")
        self.assertEqual(body["page"]["rows"][0]["値"], "249")
        self.assertIn("並び替えた上から 200件", body["page"]["note"])

    def test_画面は押せることを言う(self) -> None:
        """見出しが押せると気づかれないと、無いのと同じ。"""
        root = Path(__file__).resolve().parent.parent
        js = (root / "app" / "static" / "js" / "views" / "master.js").read_text(encoding="utf-8")
        self.assertIn('th.dataset.sortable = "1"', js)
        self.assertIn('load({ sort: "", sort_dir: "asc" })', js)    # 3回目で元の並び
        self.assertIn('"Enter"', js)                                 # キーでも押せる
        self.assertIn("見出しを押すと、その列で並び替え", js)
        css = (root / "app" / "static" / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn("th[data-sortable]:not([data-sort])::after", css)
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="tbl-sort"', page)

    def test_無い列で並べ替えても断らない(self) -> None:
        """マスタの列は上流の都合で増減する。**押しただけで断らない。**"""
        body = self.browse(file="transmission", table="作業停止時間内訳_1",
                           sort="無い列")
        self.assertEqual(body["page"]["sort"], "")
        self.assertEqual(len(body["page"]["rows"]), 2)

    # -- 関門 --------------------------------------------------------
    def test_パスワード無しでは直せない(self) -> None:
        res = self.post("/api/master/row/save", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "key": 1, "values": {"内訳": "書き換え"}})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "locked")
        self.assertEqual(self.rows()[0][0], "休憩食事")   # 動いていない

    def test_違うパスワードは403(self) -> None:
        self.assertEqual(self.unlock("ちがう").status_code, 403)
        self.assertFalse(self.browse(file="transmission")["unlocked"])

    def test_合えば開く(self) -> None:
        self.assertEqual(self.unlock().status_code, 200)
        self.assertTrue(self.browse(file="transmission")["unlocked"])

    def test_閉じられる(self) -> None:
        self.unlock()
        self.post("/api/master/unlock", {"enable": False})
        self.assertFalse(self.browse(file="transmission")["unlocked"])

    # -- 直す --------------------------------------------------------
    def test_1行を書き換える(self) -> None:
        self.unlock()
        res = self.post("/api/master/row/save", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "key": 1, "values": {"内訳": "休憩・食事"}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.rows()[0], ("休憩・食事", "0"))
        # **書いたあとも、まるごとの状態が返る**(本当に入ったかを別に訊かない)
        self.assertEqual(res.get_json()["page"]["rows"][0]["内訳"], "休憩・食事")

    def test_送っていない列は消さない(self) -> None:
        self.unlock()
        self.post("/api/master/row/save", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "key": 1, "values": {"内訳": "休憩・食事"}})
        self.assertEqual(self.rows()[0][1], "0")

    def test_1行足す(self) -> None:
        self.unlock()
        res = self.post("/api/master/row/add", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "values": {"内訳": "その他(理由記載)", "内訳番号": "ヨ"}})
        self.assertEqual(res.status_code, 200)
        self.assertIn(("その他(理由記載)", "ヨ"), self.rows())

    def test_1行消す(self) -> None:
        self.unlock()
        res = self.post("/api/master/row/delete", {
            "file": "transmission", "table": "作業停止時間内訳_1", "key": 2})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.rows()), 1)

    def test_もう無い行は409(self) -> None:
        """一覧を出したあとに誰かが消した。**押した人には見えていない事実。**"""
        self.unlock()
        res = self.post("/api/master/row/delete", {
            "file": "transmission", "table": "作業停止時間内訳_1", "key": 999})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "no_row")

    def test_数値の列に文字を入れたら400(self) -> None:
        self.unlock()
        res = self.post("/api/master/row/save", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "key": 1, "values": {"管理番号": "あいう"}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("整数", res.get_json()["error"]["message"])

    def test_無い表は404(self) -> None:
        self.unlock()
        res = self.post("/api/master/row/add", {
            "file": "transmission", "table": "無い表", "values": {"a": "1"}})
        self.assertEqual(res.status_code, 404)

    def test_直したら次に読むときに効く(self) -> None:
        """**写しの控えを捨てる。** 捨てないと、直したのに変わらない
        (読むときは共有の写しを開いているため)。"""
        self.browse(file="transmission", table="作業停止時間内訳_1")   # 写しを作らせる
        self.unlock()
        self.post("/api/master/row/save", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "key": 1, "values": {"内訳": "写しの確認"}})
        body = self.browse(file="transmission", table="作業停止時間内訳_1")
        self.assertEqual(body["page"]["rows"][0]["内訳"], "写しの確認")

    def test_停止理由の一覧にも効く(self) -> None:
        """直したものが**日報入力の選択肢に出る**。"""
        self.unlock()
        self.post("/api/master/row/add", {
            "file": "transmission", "table": "作業停止時間内訳_1",
            "values": {"内訳": "新しい理由", "内訳番号": "9"}})
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertIn("9 新しい理由", html)


class ViewOnlyTests(WebTestCase):
    """直せないファイルと、その理由。**出さないのではなく、理由を出す。**"""

    def setUp(self) -> None:
        super().setUp()
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(ref / "SIKALOT.sqlite3"))
        with conn:
            conn.execute('CREATE TABLE "仕掛" ("ﾛｯﾄ番号" TEXT)')
            conn.execute('INSERT INTO "仕掛" VALUES ("H5422S0")')
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})
        self.post("/api/master/unlock", {"enable": True, "password": "nisk"})

    def test_上流のファイルは見られるが直せない(self) -> None:
        body = self.get("/api/master/browse?file=lot").get_json()
        self.assertEqual(body["page"]["total"], 1)     # 見える
        self.assertFalse(body["page"]["editable"])     # 直せない
        self.assertIn("上流", body["page"]["why"])

    def test_パスワードを入れていても直せない(self) -> None:
        """**関門は2つで、順番に意味がある** ── 表 → パスワード。
        先にパスワードを言うと「入れれば直せる」と読ませてしまう。"""
        res = self.post("/api/master/row/save", {
            "file": "lot", "table": "仕掛", "key": 1,
            "values": {"ﾛｯﾄ番号": "X"}})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "not_editable")

    def test_日報管理は日報入力から直すと言う(self) -> None:
        body = self.get("/api/master/browse?file=access_db").get_json()
        self.assertIn("日報入力", body["page"]["why"])

    def test_一覧には直せないものも出る(self) -> None:
        """直せるものだけ出すと「あるはずのファイルが無い」に見える。"""
        body = self.get("/api/master/browse").get_json()
        keys = {f["key"]: f["editable"] for f in body["files"]}
        self.assertTrue(keys["transmission"])
        self.assertTrue(keys["material"])
        self.assertFalse(keys["lot"])
        self.assertFalse(keys["access_db"])


class MasterScreenTests(WebTestCase):
    """画面の作り。"""

    def html(self) -> str:
        return self.get("/settings").get_data(as_text=True)

    def test_表を見る面が出る(self) -> None:
        html = self.html()
        self.assertIn('id="tables"', html)
        self.assertIn("表を見る / 直す", html)
        # **選択欄2つはやめました。** 開くまで何があるか分からないので、
        # 左に一覧(`m-list`)を出して、選ぶ前に読めるようにしています
        self.assertIn('id="m-list"', html)
        self.assertIn('id="tbl-q"', html)

    def test_編集の関門は面の鍵ひとつ(self) -> None:
        """開いているかどうかが、面を開いた時点で見える。

        **合言葉の入力欄を画面に2つ置かない。** 管理者モードと「編集を
        許可する」は同じ管理者パスワードで、2つ並ぶとどちらを使うのか
        分かりません。鍵は面のいちばん上に1つだけ置きます。
        """
        html = self.html()
        self.assertIn('data-lock="master"', html)
        self.assertIn('id="lock-pw-master"', html)
        self.assertIn("鍵を開ける", html)
        self.assertNotIn('id="edit-pw"', html)

    def test_直せない理由は一覧に出す(self) -> None:
        """**出さないのではなく、理由を出す。**

        一覧はサーバが組み立てます(`master_admin.catalog`)。
        """
        body = self.get("/api/master/browse").get_json()
        groups = {g["file"]: g for g in body["catalog"]}
        self.assertTrue(groups["transmission"]["editable"])
        self.assertFalse(groups["lot"]["editable"])
        self.assertIn("上流", groups["lot"]["why"])
        # 直せるものが先。**いちばん触るものを探させない**
        self.assertTrue(body["catalog"][0]["editable"])

    def test_鍵は管理者モードでも開く(self) -> None:
        """面の上で鍵を開けたのに直せないと、効いていないとしか見えない。"""
        self.assertFalse(self.get("/api/master/browse").get_json()["unlocked"])
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertTrue(self.get("/api/master/browse").get_json()["unlocked"])

    def test_なぜここで直すのかを書いてある(self) -> None:
        self.assertIn("開く道具", self.html())


class TableNoteTests(unittest.TestCase):
    """表の説明。**このツールが名指しで読む表には、必ず一言ある。**

    説明が無くても画面は出ます ── だから抜けても気づけません。実際
    「ライン毎目標」だけ抜けたまま出ていました。**これがいちばん困る表**
    で、開いて直した人は効いたつもりで帰ります(正はCSVのほう)。

    設定に表の名前を足したら、ここで落ちます。
    """

    def test_名指しで読む表には説明がある(self) -> None:
        from nippou.config import SETTINGS
        from nippou.master_admin import TABLE_NOTES

        named = [SETTINGS.gw_material_master_table, SETTINGS.gw_vc_master_table,
                 SETTINGS.staff_master_table, SETTINGS.pack_note_table,
                 SETTINGS.line_target_table, SETTINGS.shift_time_table,
                 *SETTINGS.stop_reason_tables]
        missing = [name for name in named if not TABLE_NOTES.get(name)]
        self.assertEqual(missing, [], f"説明の無い表: {missing}")

    def test_勝ち負けのある表はそう書いてある(self) -> None:
        """**「直せる」と「効く」は別。**

        ライン毎目標は梱包資材マスタの中にあるので、編集を許可すれば
        直せてしまいます。ここを直せばグラフの45度線も動きますが、
        **同じラインが `ライン毎目標.csv` にもあればそちらが勝ちます**
        ── 直したのに出ない、が起こり得ます。名前からは読めないので、
        表の説明で言っておくしかありません。
        """
        from nippou.config import SETTINGS
        from nippou.master_admin import TABLE_NOTES

        note = TABLE_NOTES[SETTINGS.line_target_table]
        self.assertIn("勝ち", note)
        self.assertIn(SETTINGS.line_target_filename, note)

    def test_説明に飾りを書かない(self) -> None:
        """説明は**選択欄の項目名としてそのまま出ます**。

        `**…**` と書けば、アスタリスクがそのまま見えます
        (`static/js/views/settings.js` が `t.note` を文字として並べる)。
        """
        from nippou.master_admin import TABLE_NOTES

        for name, note in TABLE_NOTES.items():
            with self.subTest(table=name):
                self.assertNotIn("**", note)
                self.assertNotIn("`", note)


if __name__ == "__main__":
    unittest.main()
