"""保存前チェックの**洗い出し** ── 7項目それぞれが「発火するか」「発火して
どうなるか」

【なぜ書いたか】
7項目は `logic/save_checks.py` に揃っていて、単体テストも通っていました。
それでも現場では**素通りしていました** ── どこからも呼ばれていなかった
からです。

    この直をチェック   … 走る。並べる。**止めない**
    共有へ保存         … 走る。止める
    保存(確定)         … **走っていなかった**(行ごとの時間の断りだけ)

「書いてある」と「効いている」は別のことです。ロジックの単体テストは
前者しか見ません。ここは後者だけを見ます ── **押したときにどうなるか。**

【表】
    項目                  保存(確定)   共有へ保存   この直をチェック
    梱包数 > 検入枚数       止める       止める        並べる
    マスタに無い停止記号    止める       止める        並べる
    開始と終了が同じ        止める       止める        並べる
    1行が直より長い         止める       止める        並べる
    停止が作業時間を超える  止める       止める        並べる
    最終時間まで無い        **通す**     止める        並べる
    休憩が60分に足りない    **通す**     止める        並べる

下2つを保存(確定)で止めないのは、**直が終わっていなければ必ず足りない**
からです。8時に1行目を打った時点で断ると、誰も1行も保存できません。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord
from nippou.logic import save_checks

from tests._web import HEADERS, WebTestCase

_KEY = dict(report_date="2026年9月16日", line="L-1", shift="1直", page=1)


def _row(n: int, **kw) -> DetailRecord:
    return DetailRecord(**_KEY, row_no=n, **kw)


# ======================================================================
# 1. 判定そのもの ── 7項目が発火するか
# ======================================================================
class FiresTests(unittest.TestCase):
    """**1項目につき1つ、わざと壊した直**を通して、その項目だけが出ること。

    「出ること」だけでなく「**他が出ないこと**」も見ます ── 1つ壊したら
    3件出る、では、どれを直せばよいのか読めません。
    """

    def run_all(self, details, **kw):
        base = dict(shift="1直", shift_start="08:00", shift_end="17:00",
                    limit_minutes=540, known_stop_codes={"0", "1", "リ"},
                    day_work=False)
        base.update(kw)
        return save_checks.run_all(details, **base)

    def codes(self, details, **kw) -> set[str]:
        return {f.code for f in self.run_all(details, **kw)}

    # ---- きれいな1直。**ここが空でないと、以下は何も意味がない** ----
    def clean(self) -> list[DetailRecord]:
        return [
            _row(1, lot="A1", ken="100", kz="08", kh="00", sz="12", sh="00",
                 mai="10", tut="1", tim="240"),
            _row(2, lot="A2", ken="100", kz="12", kh="00", sz="17", sh="00",
                 mai="10", tut="1", s="0", th="60", tim="240"),
        ]

    def test_正しい直では何も出ない(self) -> None:
        self.assertEqual(self.codes(self.clean()), set())

    def test_梱包数が検入枚数を超えたら出る(self) -> None:
        rows = self.clean()
        rows[0].mai, rows[0].tut = "60", "2"      # 120 > 検入100
        self.assertEqual(self.codes(rows), {save_checks.PACK_OVER})

    def test_マスタに無い停止記号なら出る(self) -> None:
        rows = self.clean()
        rows[0].s, rows[0].th = "Ｚ", "5"
        self.assertEqual(self.codes(rows), {save_checks.UNKNOWN_STOP})

    def test_マスタが読めないときは何も言わない(self) -> None:
        """**全部を「無い記号」と言うと、マスタに届かない日に保存が止まる。**"""
        rows = self.clean()
        rows[0].s, rows[0].th = "Ｚ", "5"
        self.assertEqual(self.codes(rows, known_stop_codes=set()), set())

    def test_開始と終了が同じなら出る(self) -> None:
        rows = self.clean()
        rows[0].sz, rows[0].sh = "08", "00"
        self.assertIn(save_checks.SAME_TIME, self.codes(rows))

    def test_1行が直より長ければ出る(self) -> None:
        rows = self.clean()
        self.assertIn(save_checks.OVER_SHIFT_ROW,
                      self.codes(rows, limit_minutes=60))

    def test_停止が作業時間を超えたら出る(self) -> None:
        rows = self.clean()
        rows[0].th = "9999"
        self.assertIn(save_checks.NEGATIVE_TIME, self.codes(rows))

    def test_最終時間まで入っていなければ出る(self) -> None:
        rows = self.clean()
        rows[1].sz, rows[1].sh = "15", "30"       # 定時は17:00
        self.assertIn(save_checks.SHIFT_END, self.codes(rows))

    def test_休憩が足りなければ出る(self) -> None:
        rows = self.clean()
        rows[1].s, rows[1].th = "", ""            # 休憩(記号0)を消す
        self.assertIn(save_checks.SHORT_BREAK, self.codes(rows))

    def test_昼稼働なら休憩は見ない(self) -> None:
        rows = self.clean()
        rows[1].s, rows[1].th = "", ""
        self.assertNotIn(save_checks.SHORT_BREAK,
                         self.codes(rows, day_work=True))

    def test_作業時間の合計が直より長ければ出る(self) -> None:
        rows = self.clean()
        rows[0].tim = "9999"
        self.assertIn(save_checks.OVER_SHIFT_TOTAL, self.codes(rows))


# ======================================================================
# 2. いつ効くか ── 5つと2つの分かれ目
# ======================================================================
class WhenTests(unittest.TestCase):
    """`AT_SHIFT_END` が**最終時間と休憩のちょうど2つ**であること。"""

    def test_直の終わり向けは2つだけ(self) -> None:
        self.assertEqual(save_checks.AT_SHIFT_END,
                         {save_checks.SHIFT_END, save_checks.SHORT_BREAK})

    def test_飛ばせるものと一致する(self) -> None:
        """「設備移動のため保存」で通せる範囲と同じ。

        偶然ではありません ── どちらも「直を最後までやれたか」の話です。
        """
        rows = [_row(1, lot="A", ken="100", kz="08", kh="00", sz="15",
                     sh="30", mai="1", tut="1", tim="450")]
        found = save_checks.run_all(rows, shift="1直", shift_start="08:00",
                                    shift_end="17:00", limit_minutes=540)
        self.assertTrue(found)
        for f in found:
            with self.subTest(code=f.code):
                self.assertEqual(f.at_shift_end, f.skippable)

    def test_打っている途中は下2つを外す(self) -> None:
        rows = [_row(1, lot="A", ken="1", kz="08", kh="00", sz="09", sh="00",
                     mai="5", tut="1", tim="60")]
        found = save_checks.run_all(rows, shift="1直", shift_start="08:00",
                                    shift_end="17:00", limit_minutes=540)
        codes = {f.code for f in found}
        # 3つとも出る(梱包数超過 / 最終時間 / 休憩)
        self.assertEqual(codes, {save_checks.PACK_OVER,
                                 save_checks.SHIFT_END,
                                 save_checks.SHORT_BREAK})
        # 打っている途中で言い切れるのは梱包数だけ
        self.assertEqual({f.code for f in save_checks.during_input(found)},
                         {save_checks.PACK_OVER})

    def test_画面へ渡す形にも入っている(self) -> None:
        f = save_checks.Finding(save_checks.SHIFT_END, "x")
        self.assertTrue(f.as_dict()["at_shift_end"])
        g = save_checks.Finding(save_checks.PACK_OVER, "x")
        self.assertFalse(g.as_dict()["at_shift_end"])


# ======================================================================
# 3. 発火したあとどうなるか ── 押したときの振る舞い
# ======================================================================
class SaveGateTests(WebTestCase):
    """**保存(確定)を押したとき。** ここが素通りしていました。"""

    def _save(self, rows, **extra):
        payload = {"rows": rows, "header": {"worker": "杉山良宏"}}
        payload.update(extra)
        return self.post("/api/entry/save", payload)

    #: 通る1行。時間の断りに引っかからない形にしておく
    OK_ROW = {"LOT": "A1", "KEN": "100", "MAI": "10", "TUT": "1",
              "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}

    def test_まず通ることを確かめる(self) -> None:
        """**関門を足したせいで何も保存できない、になっていないこと。**"""
        res = self._save({"1": dict(self.OK_ROW)})
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))
        self.assertTrue(res.get_json()["saved"])

    def test_梱包数が検入枚数を超えたら保存できない(self) -> None:
        """**ここが素通りしていました。** Number_Count が止めていなかった。"""
        row = dict(self.OK_ROW, KEN="10", MAI="10", TUT="3")   # 30 > 10
        res = self._save({"1": row})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertIn("保存できません", body["message"])
        self.assertEqual([f["code"] for f in body["check_findings"]],
                         [save_checks.PACK_OVER])
        self.assertFalse(body.get("saved"))

    def test_断ったあとDBに入っていない(self) -> None:
        """**断ったのに入っていた、では関門になりません。**"""
        self._save({"1": dict(self.OK_ROW, KEN="10", MAI="10", TUT="3")})
        rows = self.repo().conn.execute(
            "SELECT COUNT(*) FROM daily_header").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_最終時間は保存では止めない(self) -> None:
        """**直が終わっていなければ必ず足りない。** 止めたら1行も打てない。"""
        res = self._save({"1": dict(self.OK_ROW)})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])

    def test_休憩も保存では止めない(self) -> None:
        res = self._save({"1": dict(self.OK_ROW, S="0", TH="5")})
        self.assertEqual(res.status_code, 200)

    def test_自動保存は止めない(self) -> None:
        """打ちかけを手元に残すためのもの。止めると何も残りません。"""
        res = self._save({"1": dict(self.OK_ROW, KEN="10", MAI="10", TUT="3")},
                         silent=True)
        self.assertEqual(res.status_code, 200)

    def test_ページをまたいで足してから見る(self) -> None:
        """**1ページだけ見ても答えが出ません。**

        1ページ目の**最後の行**に 検入10・梱包6 のロットを置き、
        2ページ目にその続き(ロット番号が空の行)で 6 を足します。
        どちらのページも単体では超えていませんが、足すと 12 > 10 です。
        """
        # 1ページ目: 11行は当たり障りのない行、12行目に見張りたいロット
        page1 = {str(n): {"LOT": f"P{n:02d}", "KEN": "100", "MAI": "1",
                          "TUT": "1", "KZ": "08", "KH": "00",
                          "SZ": "08", "SH": "05"}
                 for n in range(1, 12)}
        page1["12"] = dict(self.OK_ROW, LOT="A1", KEN="10", MAI="6",
                           TUT="1", SZ="08", SH="05")
        self.assertEqual(self._save(page1).status_code, 200)

        # 2ページ目を出す(「新規発行」。12行目まで埋まっているので通る)
        made = self.post("/api/entry/newpage",
                         {"rows": page1, "header": {"worker": "杉山良宏"}})
        self.assertEqual(made.status_code, 200, made.get_json())

        # ロット番号が空 = 1ページ目の最後のロット(A1)の続き
        cont = {"MAI": "6", "TUT": "1", "KZ": "09", "KH": "00",
                "SZ": "09", "SH": "05"}
        res = self._save({"1": cont})
        self.assertEqual(res.status_code, 422, res.get_json().get("message"))
        self.assertIn(save_checks.PACK_OVER,
                      [f["code"] for f in res.get_json()["check_findings"]])

    def test_直すと通る(self) -> None:
        """断られたあと、直せば保存できること(行き止まりにしない)。"""
        bad = dict(self.OK_ROW, KEN="10", MAI="10", TUT="3")
        self.assertEqual(self._save({"1": bad}).status_code, 422)
        good = dict(self.OK_ROW, KEN="100", MAI="10", TUT="3")
        self.assertEqual(self._save({"1": good}).status_code, 200)


class VerifyDoesNotBlockTests(WebTestCase):
    """**「この直をチェック」は並べるだけ。** 止めるのは保存のほう。"""

    def test_見つけても断りにしない(self) -> None:
        self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A1", "KEN": "100", "MAI": "10", "TUT": "1",
                           "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}},
            "header": {"worker": "杉山良宏"}})
        res = self.post("/api/entry/verify", {})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        # 最終時間(09:00 ≠ 17:00)と休憩(0分)で引っかかる ── が、断らない
        self.assertFalse(body["ok"])
        self.assertTrue(body["findings"])

    def test_直の終わり向けかどうかを画面へ渡す(self) -> None:
        """**「いま直せ」と「直の終わりに要る」は別の話。**

        並べるだけの画面でも、どちらなのかが分からないと
        「まだ8時なのに17時まで入れろと言われた」に見えます。
        """
        self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A1", "KEN": "100", "MAI": "10", "TUT": "1",
                           "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}},
            "header": {"worker": "杉山良宏"}})
        body = self.post("/api/entry/verify", {}).get_json()
        for f in body["findings"]:
            with self.subTest(code=f["code"]):
                self.assertIn("at_shift_end", f)
        self.assertTrue(any(f["at_shift_end"] for f in body["findings"]))


class ShareGateTests(WebTestCase):
    """**共有へ保存は7項目ぜんぶ。** 最終時間と休憩もここで止まります。"""

    def test_最終時間で止まる(self) -> None:
        self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A1", "KEN": "100", "MAI": "10", "TUT": "1",
                           "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}},
            "header": {"worker": "杉山良宏"}})
        res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 422)
        self.assertIn("最終時間", res.get_json()["error"]["message"])

    # ------------------------------------------------------------------
    # **逃げ道で通せないものを、名指しで返す。**
    #
    # 断り文には必ず「『設備移動のため保存』で最終時間と休憩のぶんだけ
    # 通せます」と書きます。ところが通せないもの(梱包数・停止記号)が
    # 混ざっていると、打っても通りません。画面はこれを見て、欄を
    # 打てなくしたうえで残っているものを並べます。
    # ------------------------------------------------------------------
    def test_通せるものだけなら通せると返す(self) -> None:
        self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A1", "KEN": "100", "MAI": "10", "TUT": "1",
                           "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}},
            "header": {"worker": "杉山良宏"}})
        body = self.post("/api/settings/push", {}).get_json()
        self.assertTrue(body["skippable_only"])
        self.assertEqual(body["skip_blockers"], [])

    def test_通せないものが混ざれば名指しで返す(self) -> None:
        # 梱包数 > 検入枚数。**これは「設備移動のため保存」では通らない**
        #
        # **DBへ直に入れます。** 画面から入れようとすると、保存(確定)の
        # 関門が先に断ります(それが正しい)。ここで見たいのは、すでに
        # 入っている行が共有の関門でどう返るかのほうです
        from nippou.db.models import HeaderRecord

        key = dict(report_date="2026年9月15日", line="L-1", shift="1直", page=1)
        self.repo().save(
            HeaderRecord(**key, worker="杉山良宏"),
            [DetailRecord(**key, row_no=1, lot="A1", ken="15", mai="10",
                          tut="2", kz="08", kh="00", sz="09", sh="00")])
        body = self.post("/api/settings/push", {}).get_json()
        self.assertFalse(body["skippable_only"])
        self.assertTrue(body["skip_blockers"])
        blocker = body["skip_blockers"][0]
        self.assertEqual(blocker["code"], save_checks.PACK_OVER)
        # **どの直の話かまで。** 直が複数まとまって断られることがある
        self.assertIn("L-1", blocker["where"])
        self.assertIn("1行目", blocker["where"])


class ViewingIsNotBlockedTests(WebTestCase):
    """**見に行くことは止めない。**

    1ページ目に間違いのある直で、2ページ目から1ページ目へ戻れなく
    なっていました ── ページの切り替えが先に「保存(確定)」を通して
    いたためです。**直すために開きたいのに、直っていないから開けない。**
    """

    OK_ROW = {"LOT": "A1", "KEN": "100", "MAI": "10", "TUT": "1",
              "KZ": "08", "KH": "00", "SZ": "09", "SH": "00"}

    def current_key(self) -> tuple[str, str, str]:
        """いま画面が開いている直。**日付を書き固定しない**(日が変わる)。"""
        from nippou import work_context

        with self.app.test_request_context(headers=HEADERS):
            from app.routes.entry import current_calculator
            return work_context.get_context().current_key(current_calculator())

    def _put(self, page: int, **over) -> None:
        """DBへ直に入れる(この関門より前に入った行・取り込んだ行の再現)。"""
        from nippou.db.models import DetailRecord, HeaderRecord

        date, line, shift = self.current_key()
        key = dict(report_date=date, line=line, shift=shift, page=page)
        row = dict(lot=f"L{page}", ken="15", mai="20", tut="1",
                   kz="08", kh="00", sz="09", sh="00")
        row.update(over)
        self.repo().save(HeaderRecord(**key), [DetailRecord(**key, row_no=1, **row)])

    def test_打ちかけを置くだけなら関門を通らない(self) -> None:
        """`draft` は確定ではないので、5項目でも時間の断りでも止まらない。"""
        bad = dict(self.OK_ROW, KEN="10", MAI="10", TUT="3")   # 30 > 10
        res = self.post("/api/entry/save",
                        {"rows": {"1": bad}, "header": {"worker": "杉山良宏"},
                         "draft": True})
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))
        self.assertTrue(res.get_json()["saved"])

    def test_確定はやはり止まる(self) -> None:
        """`draft` を足したせいで関門が抜けていないこと。"""
        bad = dict(self.OK_ROW, KEN="10", MAI="10", TUT="3")
        res = self.post("/api/entry/save",
                        {"rows": {"1": bad}, "header": {"worker": "杉山良宏"}})
        self.assertEqual(res.status_code, 422)

    def test_打ちかけは間引かれない(self) -> None:
        """自動保存と違って、移る前の1回は待たされては困ります。"""
        body = {"rows": {"1": dict(self.OK_ROW)},
                "header": {"worker": "杉山良宏"}}
        self.assertTrue(self.post("/api/entry/save",
                                  {**body, "draft": True}).get_json()["saved"])
        # 続けてもう1度。自動保存なら「まだその時ではない」で見送られる
        second = self.post("/api/entry/save", {**body, "draft": True}).get_json()
        self.assertTrue(second["saved"], second.get("skipped"))

    def test_間違いのあるページへ戻れる(self) -> None:
        """**ここが通らなくなっていました。**"""
        self._put(1)                 # 検入15 に 梱包20 ── 間違いのあるページ
        self._put(2, lot="L2", ken="100", mai="1")
        self.post("/api/settings/page", {"page": 2})

        moved = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(moved.status_code, 200, moved.get_json())
        html = self.get("/").get_data(as_text=True)
        self.assertIn('value="L1"', html)                        # 1ページ目のロット(ライン名ではない)


class NewPageGateTests(WebTestCase):
    """**新規発行も確定の入口です。** ここが素通りしていました。

    「いまのページを保存してから次を作る」ので、関門が無いと
    **間違いのあるページがそのまま確定され、次のページが出ます。**
    """

    def _newpage(self, first):
        """12行ぜんぶ埋めて発行する。

        「次ページ発行」は**12行目まで埋まってから**しか通りません
        (`logic/pages.new_page_check`)。ここで見たいのはその先の
        保存前チェックなので、行数のほうは満たしておきます。
        """
        # **1行5分。** 12行×60分だと合計720分で、直の540分を超えて
        # 別の断り(作業時間の合計)に当たります
        rows = {str(n): {"LOT": f"F{n:02d}", "KEN": "100", "MAI": "1",
                         "TUT": "1", "KZ": "08", "KH": "00",
                         "SZ": "08", "SH": "05"}
                for n in range(2, 13)}
        rows["1"] = first
        return self.post("/api/entry/newpage",
                         {"rows": rows, "header": {"worker": "杉山良宏"},
                          "confirm": True})

    def test_間違いがあれば新しいページを出せない(self) -> None:
        res = self._newpage({"LOT": "H573C51", "KEN": "15", "MAI": "20",
                             "TUT": "1", "KZ": "08", "KH": "00",
                             "SZ": "08", "SH": "05"})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertIn("新しいページを出せません", body["message"])
        self.assertEqual([f["code"] for f in body["check_findings"]],
                         [save_checks.PACK_OVER])
        # 1ページ目も確定されていない
        rows = self.repo().conn.execute(
            "SELECT COUNT(*) FROM daily_header").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_直っていれば出せる(self) -> None:
        res = self._newpage({"LOT": "H573C51", "KEN": "100", "MAI": "20",
                             "TUT": "1", "KZ": "08", "KH": "00",
                             "SZ": "08", "SH": "05"})
        self.assertEqual(res.status_code, 200, res.get_json())


class AllStopTests(WebTestCase):
    """全停入力 ── **記号を書く。休憩は見ない。**

    名前("ﾌｫｰｸ待ち")を欄に入れていたので、保存前チェックが
    「停止内訳にない記号です」で断り、**そのページは以後どうやっても
    保存できなく**なっていました(消した形を保存するところで同じ断りに
    当たるため)。全停は直に1回きりなので、やり直しもできません。
    """

    def test_全停の直は休憩を求めない(self) -> None:
        """**止まっているのだから、休憩も何もありません。**"""
        from nippou.db.models import DetailRecord
        from nippou.logic import save_checks

        row = DetailRecord(report_date="2026年9月16日", line="L-1",
                           shift="1直", page=1, row_no=1,
                           kz="08", kh="00", sz="17", sh="00",
                           s="1", th="540", tim="540")
        found = save_checks.run_all([row], shift="1直", shift_start="08:00",
                                    shift_end="17:00", limit_minutes=540,
                                    known_stop_codes={"0", "1"})
        self.assertNotIn(save_checks.SHORT_BREAK, [f.code for f in found])

    def test_ふつうの直では休憩を求める(self) -> None:
        """全停でないときまで見逃さないこと。"""
        from nippou.db.models import DetailRecord
        from nippou.logic import save_checks

        row = DetailRecord(report_date="2026年9月16日", line="L-1",
                           shift="1直", page=1, row_no=1, lot="A1",
                           kz="08", kh="00", sz="17", sh="00", tim="540")
        found = save_checks.run_all([row], shift="1直", shift_start="08:00",
                                    shift_end="17:00", limit_minutes=540)
        self.assertIn(save_checks.SHORT_BREAK, [f.code for f in found])

    def test_書かれるのは記号で名前ではない(self) -> None:
        res = self.post("/api/formstop/execute",
                        {"reason": "ﾌｫｰｸ待ち", "code": "ニ",
                         "worker": "山田"})
        self.assertEqual(res.status_code, 200, res.get_json())
        rows = self.repo().conn.execute(
            "SELECT s FROM daily_detail WHERE row_no=1").fetchall()
        self.assertEqual([r[0] for r in rows], ["ニ"])

    def test_全停で入れたページは保存し直せる(self) -> None:
        """**ここが手詰まりの正体でした。**

        名前が入っていた頃は、全停で作ったページを開いて保存すると
        「停止内訳にない記号です」で断られ、消すこともできませんでした。
        """
        self.post("/api/formstop/execute",
                  {"reason": "ﾌｫｰｸ待ち", "code": "0", "worker": "山田"})
        res = self.post("/api/entry/save", {
            "rows": {"1": {"KZ": "08", "KH": "00", "SZ": "17", "SH": "00",
                           "S": "0", "TH": "540"}},
            "header": {"worker": "杉山良宏"}})
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))


class PendingCountTests(WebTestCase):
    """**「共有へ未保存 ◯直」の数。** 数えていたのはページでした。

    1直を4ページ打つと「4直」と出ていました ── 送るのは直の単位
    (`shift_check.run_pending` が直でまとめる)なので、4回押す必要が
    あるように読めます。
    """

    def _put(self, page: int, shift: str = "1直") -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        key = dict(report_date="2026年9月16日", line="L-1", shift=shift,
                   page=page)
        self.repo().save(HeaderRecord(**key),
                         [DetailRecord(**key, row_no=1, lot=f"L{page}")])

    def test_1直を4ページ打っても1直(self) -> None:
        for page in (1, 2, 3, 4):
            self._put(page)
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("<b>1直ぶん</b>", html)
        # **面の見出しにも同じ数。** 本文だけ直して見出しが4のままだった
        self.assertIn("共有へ未送信 1直ぶん", html)
        self.assertNotIn("4直", html)

    def test_直が違えば数も増える(self) -> None:
        self._put(1, "1直")
        self._put(1, "2直")
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("<b>2直ぶん</b>", html)

    def test_レールのバッジも直で数える(self) -> None:
        for page in (1, 2, 3):
            self._put(page)
        html = self.get("/").get_data(as_text=True)
        # **「3直」は第3直に読めます。** 数だと分かる形にする
        self.assertIn("共有へ未送信 1直ぶん", html)
        self.assertNotIn("3直ぶん", html)


class UnskippableTests(unittest.TestCase):
    """**逃げ道で通せるかどうかの仕分け**(`save_checks.unskippable`)。"""

    def test_通せるものは残らない(self) -> None:
        found = [save_checks.Finding(save_checks.SHIFT_END, "最終時間",
                                     skippable=True),
                 save_checks.Finding(save_checks.SHORT_BREAK, "休憩",
                                     skippable=True)]
        self.assertEqual(save_checks.unskippable(found), [])

    def test_通せないものだけ残る(self) -> None:
        over = save_checks.Finding(save_checks.PACK_OVER, "梱包数")
        found = [save_checks.Finding(save_checks.SHIFT_END, "最終時間",
                                     skippable=True), over]
        self.assertEqual(save_checks.unskippable(found), [over])

    def test_空なら空(self) -> None:
        self.assertEqual(save_checks.unskippable([]), [])


class WorkerFirstTests(WebTestCase):
    """**作業者を決めるまで、紙が増える操作は押せない。**

    「作業者を選ぶ前に全停入力できてしまう」と言われたところです。
    全停入力も次ページ発行も、押した時点で1ページを書いて保存するので、
    作業者が空のまま通すと「誰の直か分からない紙」が1枚できます。

    画面側は伏せるだけ(`paintNeedsWorker`)、断るのはサーバです。
    """

    def test_伏せる印が画面に付いている(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="open-formstop"', html)
        # 印は1か所ではなく、紙が増える操作ぜんぶに付ける
        self.assertGreaterEqual(html.count("data-need-worker"), 2)

    def test_作業者が空なら画面がそう言う(self) -> None:
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": ""}, "checks": {}}).get_json()
        self.assertTrue(body["needs_worker"])

    def test_作業者を入れれば解ける(self) -> None:
        body = self.post("/api/entry/state", {
            "rows": {}, "header": {"worker": "杉山良宏"},
            "checks": {}}).get_json()
        self.assertFalse(body["needs_worker"])


if __name__ == "__main__":
    unittest.main()
