"""参照パスとマスタ管理 (設定画面)

**関門はサーバが持つ。** 「参照パスを変えるには管理者パスワードが要る」
という判断は `nippou/presenters/settings.py` の1か所にあり、画面は
断りの理由を出すだけ ── ここではその境界が守られているかを見る。

【なぜ参照パスだけ守るのか】
参照パスを変えると、このツールが読みに行く相手そのものが変わる。
間違った先を指したまま使うと、画面はふつうに出るのに中身だけが別物に
なり、気づくのは製品が出たあとになる。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.presenters import settings as settings_presenter
from tests._web import HEADERS, WebTestCase


class PathViewTests(WebTestCase):
    def test_設定画面に参照パスが出る(self) -> None:
        """**欄そのものが出ていること**を見る。

        文言だけを見ていると、説明文のどこかに同じ言葉があるだけで
        通ってしまいます(実際、面に分けたあとも説明文に当たって
        通っていました)。欄の `id` で見ます。
        """
        from nippou import config

        body = self.get("/settings").get_data(as_text=True)
        # 出すのは**フォルダだけ**(ファイル名は既定で足ります)
        for key in settings_presenter.SHOWN_PATH_KEYS:
            with self.subTest(key=key):
                self.assertIn(f'id="path-{key}"', body)
        self.assertIn("共有の日報管理のパス", body)
        self.assertIn("参照用マスタの参照パス", body)
        # 設定そのものは全部残っている
        self.assertEqual(
            {v["key"] for v in self.get("/api/settings/state").get_json()["paths"]},
            set(config.PATH_KEYS))

    def test_設定した値と実際に見る道を両方出す(self) -> None:
        """片方だけだと、相対で書いたときにどこを見ているのか分からない。"""
        state = self.get("/api/settings/state").get_json()
        for view in state["paths"]:
            self.assertIn("value", view)
            self.assertIn("resolved", view)

    def test_説明に飾りを書かない(self) -> None:
        """参照パスの説明は**そのまま画面に出る文字**です。

        `**…**` と書けば、アスタリスクがそのまま見えます ── ソースの
        コメントの調子で書いてしまいがちなので、縛ります
        (実際に `ここが唯一の橋` が画面でアスタリスク付きで出ていました)。
        """
        from nippou.presenters.settings import FILE_USES, PATH_FIELDS

        for _key, label, note in PATH_FIELDS:
            with self.subTest(path=label):
                self.assertNotIn("**", label)
                self.assertNotIn("**", note)
        for key, uses in FILE_USES.items():
            for table, use in uses:
                with self.subTest(file=key, table=table):
                    self.assertNotIn("**", table)
                    self.assertNotIn("**", use)

    def test_パスは全部に印が付く(self) -> None:
        """「パスの変更は全部パスワードを必須にしておいてください」。"""
        state = self.get("/api/settings/state").get_json()
        protected = {v["key"]: v["protected"] for v in state["paths"]}
        for key in protected:
            with self.subTest(key=key):
                self.assertTrue(protected[key], f"{key} が守られていません")


class PasswordGateTests(WebTestCase):
    """「パスがないと変更ができない」。"""

    def test_パスワード無しで参照パスを変えると403(self) -> None:
        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": str(self.tmp / "ref")})
        self.assertEqual(res.status_code, 403)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "need_password")
        # **何が要るか**を言う。何が違うのかは言わない(総当たりの手がかり)
        self.assertIn("管理者パスワード", body["error"]["message"])
        self.assertEqual(body["changing"], ["参照用マスタの参照パス"])

    def test_書き先のファイル名も守る(self) -> None:
        """拡張子で書き先の形式(.accdb か .sqlite3 か)が決まる。

        置き場所を変えるのと同じ重さなので、同じ関門で守る。
        """
        res = self.post("/api/settings/paths",
                        {"access_db_filename": "日報管理.sqlite3"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["changing"], ["日報データのファイル名"])

    def test_書き先をsqlite3にできる(self) -> None:
        res = self.post("/api/settings/paths",
                        {"access_db_filename": "日報管理.sqlite3",
                         "password": "nisk"})
        self.assertEqual(res.status_code, 200)
        target = next(f for f in res.get_json()["files"] if f["key"] == "access_db")
        self.assertEqual(target["kind"], "sqlite3")
        self.assertTrue(target["writable"])
        # **まだ無くてよい。** 初回の保存で作られる
        self.assertFalse(target["exists"])
        self.assertIn("初回の保存", target["error"])

    def test_Accessにすれば書けない扱い(self) -> None:
        """`.accdb` は Windows と ODBC ドライバが要る。画面でそう示す。

        既定は `.sqlite3` になったので、**わざわざ `.accdb` にしたとき**の
        話になる ── フォルダに `.accdb` しか置いていない現場がここ。
        """
        res = self.post("/api/settings/paths",
                        {"access_db_filename": "日報管理.accdb", "password": "nisk"})
        self.assertEqual(res.status_code, 200)
        target = next(f for f in self.get("/api/settings/state").get_json()["files"]
                      if f["key"] == "access_db")
        self.assertEqual(target["kind"], "access")
        self.assertFalse(target["writable"])

    def test_知らない拡張子は断る(self) -> None:
        # 打ち間違いをそのまま保存すると、反映のたびに失敗して原因が分からない
        res = self.post("/api/settings/paths",
                        {"access_db_filename": "日報管理.txt", "password": "nisk"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("拡張子", res.get_json()["error"]["message"])

    def test_フォルダを混ぜたら断る(self) -> None:
        res = self.post("/api/settings/paths",
                        {"access_db_filename": "sub/日報管理.sqlite3",
                         "password": "nisk"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("ファイル名だけ", res.get_json()["error"]["message"])

    def test_断られたら1つも書かれていない(self) -> None:
        """途中まで書いてから断ると、直せない状態が残る。"""
        target = str(self.tmp / "ref")
        self.post("/api/settings/paths",
                  {"gw_reference_dir": target, "sound_dir": str(self.tmp / "snd")})
        state = self.get("/api/settings/state").get_json()
        values = {v["key"]: v["value"] for v in state["paths"]}
        self.assertEqual(values["gw_reference_dir"], "")
        self.assertEqual(values["sound_dir"], "", "断ったのに一部だけ書かれています")

    def test_パスワードが合えば変えられる(self) -> None:
        target = self.tmp / "ref"
        target.mkdir()
        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": str(target), "password": "nisk"})
        self.assertEqual(res.status_code, 200)
        state = res.get_json()
        picked = next(v for v in state["paths"] if v["key"] == "gw_reference_dir")
        self.assertEqual(picked["resolved"], str(target))
        self.assertTrue(picked["exists"])

    def test_違うパスワードは403(self) -> None:
        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": str(self.tmp / "ref"),
                         "password": "ちがう"})
        self.assertEqual(res.status_code, 403)

    def test_パスはどれもパスワードが要る(self) -> None:
        """**全部**です(v3.49.0)。音の置き場所も、出力先も。"""
        from nippou import config

        for key in config.PATH_KEYS:
            with self.subTest(key=key):
                value = ("べつ.csv" if key.endswith("_file")
                         else "べつ.sqlite3" if key.endswith("_filename")
                         else str(self.tmp / f"別-{key}"))
                res = self.post("/api/settings/paths", {key: value})
                self.assertEqual(res.status_code, 403)

    def test_音のファイル名はパスワード不要(self) -> None:
        """**守るのはパスだけ。** 名前は間違えても鳴らないだけです。"""
        res = self.post("/api/settings/paths",
                        {"sound_file_print_reminder": "べつの音.wav"})
        self.assertEqual(res.status_code, 200)

    def test_値が変わらない保存では聞かない(self) -> None:
        """画面は参照パスを毎回まとめて送る。変わらないぶんで聞かない。

        **ここは全部守るようにしても変わりません。** 変わらないぶんで
        聞くと、1つ直すたびに全部ぶんのパスワードを求めることになります。
        """
        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": "", "sound_dir": ""})
        self.assertEqual(res.status_code, 200)

    def test_知らない項目は400(self) -> None:
        res = self.post("/api/settings/paths", {"nope": "x"})
        self.assertEqual(res.status_code, 400)

    def test_空の本文は400(self) -> None:
        self.assertEqual(self.post("/api/settings/paths", {}).status_code, 400)


class AdminPasswordTests(WebTestCase):
    def test_既定のパスワードで管理者になれる(self) -> None:
        res = self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["admin"])

    def test_変えたら新しいほうで通り古いほうでは通らない(self) -> None:
        res = self.post("/api/settings/admin-password",
                        {"current": "nisk", "new": "kaeta", "confirm": "kaeta"})
        self.assertEqual(res.status_code, 200)

        self.assertEqual(
            self.post("/api/settings/admin",
                      {"enable": True, "password": "nisk"}).status_code, 403)
        self.assertEqual(
            self.post("/api/settings/admin",
                      {"enable": True, "password": "kaeta"}).status_code, 200)

    def test_変えたパスワードで参照パスも変えられる(self) -> None:
        """関門は1つ。**照合の出どころが分かれていないこと。**"""
        self.post("/api/settings/admin-password",
                  {"current": "nisk", "new": "kaeta", "confirm": "kaeta"})
        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": str(self.tmp / "ref"),
                         "password": "kaeta"})
        self.assertEqual(res.status_code, 200)

    def test_平文で保存しない(self) -> None:
        from nippou import user_settings

        self.post("/api/settings/admin-password",
                  {"current": "nisk", "new": "himitsu", "confirm": "himitsu"})
        stored = user_settings.get("admin_password")
        self.assertNotIn("himitsu", stored)
        self.assertTrue(stored.startswith("pbkdf2$"))

    def test_いまのが違えば変えられない(self) -> None:
        # 肩越しに見ていた人が勝手に変えられる、を作らない
        res = self.post("/api/settings/admin-password",
                        {"current": "ちがう", "new": "aaaa", "confirm": "aaaa"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "wrong_password")

    def test_確認用と一致しなければ断る(self) -> None:
        res = self.post("/api/settings/admin-password",
                        {"current": "nisk", "new": "aaaa", "confirm": "bbbb"})
        self.assertEqual(res.get_json()["error"]["code"], "mismatch")

    def test_短すぎれば断る(self) -> None:
        res = self.post("/api/settings/admin-password",
                        {"current": "nisk", "new": "a", "confirm": "a"})
        self.assertEqual(res.get_json()["error"]["code"], "too_short")

    def test_既定に戻せる(self) -> None:
        self.post("/api/settings/admin-password",
                  {"current": "nisk", "new": "kaeta", "confirm": "kaeta"})
        res = self.post("/api/settings/admin-password",
                        {"reset": True, "current": "kaeta"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            self.post("/api/settings/admin",
                      {"enable": True, "password": "nisk"}).status_code, 200)

    def test_非ASCIIのパスワードでも500にしない(self) -> None:
        # `compare_digest` に str を渡すと TypeError で500になる
        res = self.post("/api/settings/admin",
                        {"enable": True, "password": "にほんご"})
        self.assertEqual(res.status_code, 403)


class MasterAdminTests(WebTestCase):
    """マスタ管理 ── そのフォルダに何があり、読めるか。"""

    def _make_master(self) -> Path:
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        path = ref / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.execute('CREATE TABLE "資材重量" ("梱包資材名" TEXT, "単位質量" REAL)')
        conn.execute('INSERT INTO "資材重量" VALUES (?, ?)', ("木枠", 1.5))
        conn.commit()
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})
        return path

    def test_無いファイルは無いと言う(self) -> None:
        state = self.get("/api/settings/state").get_json()
        material = next(f for f in state["files"] if f["key"] == "material")
        self.assertFalse(material["exists"])
        self.assertEqual(material["error"], "ファイルがありません")

    def test_sqlite3があれば読める(self) -> None:
        self._make_master()
        state = self.get("/api/settings/state").get_json()
        material = next(f for f in state["files"] if f["key"] == "material")
        self.assertTrue(material["exists"])
        self.assertTrue(material["readable"])
        self.assertEqual(material["kind"], "sqlite3")
        self.assertEqual(material["tables"], ["資材重量"])
        # **写しから読んでいること。** 共有のファイルは開かない
        self.assertTrue(material["copied_to"])

    def test_中身を見られる(self) -> None:
        self._make_master()
        body = self.get("/api/master/browse?file=material").get_json()
        page = body["page"]
        self.assertEqual(page["error"], "")
        self.assertEqual(body["table"], "資材重量")
        self.assertEqual(page["columns"], ["梱包資材名", "単位質量"])
        self.assertEqual([{k: v for k, v in r.items() if k != body["row_key"]}
                          for r in page["rows"]],
                         [{"梱包資材名": "木枠", "単位質量": "1.5"}])

    def test_Accessのマスタは中身を出さない(self) -> None:
        # 開くには Windows と ODBC ドライバが要る。**開けない理由を返す**
        self.post("/api/settings/paths",
                  {"access_db_filename": "日報管理.accdb", "password": "nisk"})
        body = self.get("/api/master/browse?file=access_db").get_json()
        self.assertIn("Access", body["page"]["error"])

    def test_知らないテーブルは理由を返す(self) -> None:
        self._make_master()
        body = self.get("/api/master/browse?file=material&table=無い").get_json()
        self.assertIn("ありません", body["page"]["error"])
        # **選び直せるように、ある表は返す**
        self.assertEqual([t["table"] for t in body["tables"]], ["資材重量"])

    def test_見られなくても500にしない(self) -> None:
        """マスタが読めないことと、アプリが使えないことは別。"""
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        (ref / "梱包資材マスタ.sqlite3").write_bytes(b"not a database")
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(ref), "password": "nisk"})
        self.assertEqual(self.get("/settings").status_code, 200)
        res = self.get("/api/master/browse?file=material")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["page"]["error"])

    def test_sqlite3があればAccessより先に見る(self) -> None:
        """上流が移った日に、設定を触らせない。"""
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        (ref / "梱包資材マスタ.accdb").write_bytes(b"x")
        self._make_master()
        state = self.get("/api/settings/state").get_json()
        material = next(f for f in state["files"] if f["key"] == "material")
        self.assertTrue(material["name"].endswith(".sqlite3"))


class FsBrowseTests(WebTestCase):
    """フォルダを辿る。ブラウザのダイアログはサーバ側のフォルダを選べない。"""

    def test_フォルダを一覧できる(self) -> None:
        (self.tmp / "共有").mkdir()
        (self.tmp / "共有" / "中").mkdir()
        body = self.get(f"/api/fs/list?path={self.tmp / '共有'}").get_json()
        self.assertTrue(body["exists"])
        self.assertEqual(body["dirs"], ["中"])

    def test_取り込み元らしいファイルだけ出す(self) -> None:
        (self.tmp / "共有").mkdir()
        (self.tmp / "共有" / "a.sqlite3").write_bytes(b"x")
        (self.tmp / "共有" / "b.accdb").write_bytes(b"x")
        (self.tmp / "共有" / "c.txt").write_text("x")
        body = self.get(f"/api/fs/list?path={self.tmp / '共有'}").get_json()
        self.assertEqual(sorted(f["name"] for f in body["files"]),
                         ["a.sqlite3", "b.accdb"])

    def test_中身は返さない(self) -> None:
        """読み取り口をここに作らない。"""
        (self.tmp / "共有").mkdir()
        (self.tmp / "共有" / "a.sqlite3").write_bytes("ひみつ".encode("utf-8"))
        body = self.get(f"/api/fs/list?path={self.tmp / '共有'}").get_json()
        self.assertEqual(set(body["files"][0]), {"name", "size"})

    def test_無いフォルダでも親は返す(self) -> None:
        # 打ち間違えたときに、一段戻って選び直せる
        body = self.get(f"/api/fs/list?path={self.tmp / '無い'}").get_json()
        self.assertFalse(body["exists"])
        self.assertEqual(body["parent"], str(self.tmp))

    def test_トークンが要る(self) -> None:
        # フォルダの名前もこの端末の情報。素通しにはしない
        res = self.client.get("/api/fs/list", headers={"Host": "127.0.0.1"})
        self.assertEqual(res.status_code, 401)


class RelativePathTests(WebTestCase):
    """相対で書いたら**アプリのフォルダから**たどる。

    「いまの作業フォルダ」から見ると、どこから起動したかで指す先が変わる。
    """

    def test_相対はアプリのフォルダから(self) -> None:
        from nippou import app_config

        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": "data/ref", "password": "nisk"})
        picked = next(v for v in res.get_json()["paths"]
                      if v["key"] == "gw_reference_dir")
        self.assertEqual(picked["resolved"], str(app_config.APP_ROOT / "data/ref"))
        # **どちらとして読んだか**を画面に出す
        self.assertTrue(picked["is_relative"])

    def test_空にすると既定に戻る(self) -> None:
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(self.tmp / "ref"), "password": "nisk"})
        res = self.post("/api/settings/paths",
                        {"gw_reference_dir": "", "password": "nisk"})
        picked = next(v for v in res.get_json()["paths"]
                      if v["key"] == "gw_reference_dir")
        self.assertTrue(picked["is_default"])


class ExplainedActionsTests(WebTestCase):
    """**ボタンの名前だけでは何をするのか読めなかった。**

    もとの名前は「Accessへ反映」でした。やっていることは
    「この端末に溜まった日報を、みんなが見る側へ書き写す」── つまり
    **保存**です。「反映」はソースの中の言い方(③反映)で、押す人の
    言葉ではありませんでした。

    保存先が2つある(手元 / 共有)ことも名前からは分からず、
    **「Access と sqlite3 の両方へ書くのか」**と読めてしまいます。
    書き先は1つだけです。ここはその2つと、直の境界時刻が何を決めて
    いるかを守ります。
    """

    def html(self) -> str:
        return self.get("/settings").get_data(as_text=True)

    def test_ボタンは保存と名乗る(self) -> None:
        """やっていることは保存。「反映」では何をするのか読めない。"""
        html = self.html()
        self.assertIn("共有へ保存", html)
        self.assertNotIn("Accessへ反映", html)
        self.assertNotIn("日報管理へ反映", html)

    def test_保存先を2つとも出す(self) -> None:
        """**手元と共有は別のファイル。** 混ぜると「入力したのに共有に
        出ていない」が説明できない。"""
        from nippou.config import SETTINGS

        html = self.html()
        self.assertIn(str(SETTINGS.sqlite_path), html)      # 手元
        self.assertIn(str(SETTINGS.access_db_path), html)   # 共有

    def test_共有へ書くのは1つだけと書いてある(self) -> None:
        """Access と sqlite3 の両方へ書くわけではない。"""
        html = self.html()
        self.assertIn("両方へ書くことはありません", html)

    def test_既定の共有先はsqlite3と出る(self) -> None:
        self.assertIn("sqlite3", self.html())

    def test_共有先をaccdbにすればAccessと出る(self) -> None:
        self.post("/api/settings/paths",
                  {"access_db_filename": "日報管理.accdb", "password": "nisk"})
        html = self.html()
        self.assertIn("Access(.accdb)", html)
        # 書き方が変わってもボタンは「保存」のまま
        self.assertIn("共有へ保存", html)

    def test_溜めてまとめて書くことを書いてある(self) -> None:
        """入力のたびに共有へ行かない、という前提が読めないと、
        「共有へ未保存 3 件」が壊れているように見える。"""
        html = self.html()
        self.assertIn("共有へ未送信", html)
        self.assertIn("手元に貯めて", html)

    def test_直の境界時刻が何を決めているかを書いてある(self) -> None:
        html = self.html()
        self.assertIn("時間用", html)
        for driven in ("いま何直か", "どの日の日報か", "終了時刻の引き継ぎ",
                       "印刷の催促", "作業時間の上限"):
            with self.subTest(driven=driven):
                self.assertIn(driven, html)


if __name__ == "__main__":
    unittest.main()


class IsolationTests(WebTestCase):
    """**テストは自分の一時フォルダの外へ書かない。**

    目標CSVの見本を出すテストが `~/NippouGwRef` へ書いていました
    ── 動かした人の本物のフォルダです。既定の置き場所は「何も設定して
    いないとき」の道なので、テストの環境変数で寄せておかないと、
    書き出しを伴うテストが全部そこへ落ちます。
    """

    def test_参照用マスタの既定が一時フォルダの中にある(self) -> None:
        from nippou.config import SETTINGS

        for path in (SETTINGS.gw_reference_dir, SETTINGS.line_target_path,
                     SETTINGS.sqlite_path, SETTINGS.report_output_dir):
            with self.subTest(path=str(path)):
                self.assertTrue(str(path).startswith(str(self.tmp)),
                                f"一時フォルダの外を指しています: {path}")


class SettingsWordingTests(WebTestCase):
    """「何してるのかよくわかりません」と言われたところ。

    どちらも**押す前に読む文**です。押したあとの結果だけでは、何が
    起きたのかを組み立て直すことになります。
    """

    def html(self) -> str:
        return self.client.get("/settings", headers=HEADERS).get_data(as_text=True)

    def test_集計を作り直すが何をするか書いてある(self) -> None:
        html = self.html()
        start = html.index("集計を作り直す")
        card = html[start:html.index("rebuild-note", start)]
        for word in ("読むもの", "書き換えるもの", "変わって見えるところ",
                     "変わらないもの"):
            with self.subTest(word=word):
                self.assertIn(word, card)
        # **共有へは送りません** ── いちばん誤解されると重いところ
        self.assertIn("共有へは送りません", card)
        # 打った12行は触らない
        self.assertIn("1文字も触りません", card)

    def test_取り込みの重複がどうなるか書いてある(self) -> None:
        """「重複だった場合どうなるんですか？」── まるごと入れ替えです。"""
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("中身をまるごと入れ替えます", js)
        self.assertIn("いまある行は消えて", js)
        # 「入る先」の2文字では、何の一覧なのかが分からない
        self.assertNotIn("<th>入る先</th>", js)
        self.assertIn("入れる日報(日付・ライン・直・ページ)", js)

    def test_別の面へ連れて行ける(self) -> None:
        """日報入力の「見るだけです」から、管理者モードまで運びます。"""
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn('URLSearchParams(location.search).get("tab")', js)
