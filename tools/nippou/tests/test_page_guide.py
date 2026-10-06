"""次ページの出し方と、前のページの直し方 (v4.8.0 / v4.9.0 で1行ずつに)

    日報入力でページを増やす際は12行目が埋まっていることをチェック
    12行目まで埋まった際は次ページをどう出すかの案内
    また戻って直したい際の案内
    無ければ実装あれば再チェックしてください

**チェックはありましたが、すり抜けていました。** 11行目の終了を打つと、
その時刻が12行目の開始へ自動で写ります。前は「12行目に何か入っていれば
埋まっている」と見ていたので、11行しか打っていなくても次のページが出せ、
画面も「12行を使い切りました」と出していました(本物のブラウザで確かめて
分かった)。12行目は**終了まで入って**初めて埋まった、と見ます。

ここで押さえるのは:

    1. 12行目の見分け(空 / 開始時刻だけ / 途中 / 終了まで)と、それぞれの断り
    2. 12行目より上の空いた行は断らずに確かめる(聞くのは1回にまとめる)
    3. 表の上の案内: 12行目まで済んだら次ページの出し方、前のページを直して
       いるあいだは直し方と戻り方
    4. ページを移ったときの言葉が、画面のボタンの名前と合っている
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants  # noqa: E402
from nippou.logic import pages  # noqa: E402
from nippou.presenters import entry as presenter  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DONE = {"LOT": "N7131T0", "KZ": "08", "KH": "00", "SZ": "08", "SH": "15"}


def full_rows(last=None, upto=12) -> dict[int, dict[str, str]]:
    """1〜11行目は終了まで、12行目は `last`(省略なら終了まで)。"""
    rows = {r: dict(DONE, LOT=f"N71{r:02d}T0") for r in range(1, upto)}
    rows[12] = dict(DONE, LOT="N7112T0") if last is None else dict(last)
    return rows


class RowStateTests(unittest.TestCase):
    def test_4つに見分ける(self) -> None:
        self.assertEqual(pages.row_state({}), pages.ROW_EMPTY)
        self.assertEqual(pages.row_state({"KZ": "13", "KH": "00"}), pages.ROW_START_ONLY)
        self.assertEqual(pages.row_state({"LOT": "N7131T0"}), pages.ROW_UNFINISHED)
        self.assertEqual(pages.row_state({"KZ": "13", "KH": "00", "SZ": "13"}),
                         pages.ROW_UNFINISHED, "終了の時だけでは終わっていない")
        self.assertEqual(pages.row_state(DONE), pages.ROW_FILLED)

    def test_全停の行は終了まで入っている(self) -> None:
        """全停入力の1行目はロットを持たない。"""
        self.assertEqual(pages.row_state({"KZ": "22", "KH": "50", "SZ": "07", "SH": "00",
                                          "S": "0"}), pages.ROW_FILLED)

    def test_停止の記号だけの行は数えない(self) -> None:
        self.assertFalse(pages.row_counts(pages.row_state({"S": "0"})))

    def test_空白は空(self) -> None:
        self.assertEqual(pages.row_state({"LOT": "  ", "SZ": " "}), pages.ROW_EMPTY)


class NewPageCheckTests(unittest.TestCase):
    def test_12行目が終了まで入っていれば出せる(self) -> None:
        got = pages.new_page_check(full_rows())
        self.assertTrue(got.ready)
        self.assertEqual((got.refusal, got.used, got.gaps), ("", 12, ()))

    def test_空のページは断る(self) -> None:
        got = pages.new_page_check({})
        self.assertFalse(got.ready)
        self.assertIn("まだ空", got.refusal)

    def test_12行目が空なら断る(self) -> None:
        got = pages.new_page_check(full_rows(last={}))
        self.assertFalse(got.ready)
        self.assertIn("12行目まで打ってから", got.refusal)
        self.assertIn("11/12行", got.refusal)

    def test_写っただけの開始時刻では出せない(self) -> None:
        """**これがすり抜けていた。** 11行目の終了が12行目の開始へ写る。"""
        got = pages.new_page_check(full_rows(last={"KZ": "08", "KH": "15"}))
        self.assertFalse(got.ready)
        self.assertIn("開始時刻だけ", got.refusal)
        self.assertIn("11行目の終了から", got.refusal)
        self.assertNotIn("\n", got.refusal, "断りも1行で")
        self.assertEqual(got.used, 11)

    def test_12行目の終了がまだなら断る(self) -> None:
        got = pages.new_page_check(full_rows(last={"LOT": "N7112T0", "KZ": "08", "KH": "15"}))
        self.assertFalse(got.ready)
        self.assertIn("終了時刻がまだ", got.refusal)

    def test_上に空いた行があっても断らず_確かめる(self) -> None:
        rows = full_rows()
        rows[3] = {}
        rows[7] = {"KZ": "09", "KH": "00"}            # 開始時刻だけも空と同じ
        got = pages.new_page_check(rows)
        self.assertTrue(got.ready)
        self.assertEqual(got.gaps, (3, 7))
        self.assertIn("3行目・7行目が空のまま", got.gap_question())

    def test_空いた行が無ければ聞かない(self) -> None:
        self.assertEqual(pages.new_page_check(full_rows()).gap_question(), "")

    def test_押したときの確かめは1行(self) -> None:
        """長いと読まれない(v4.9.0)。引き継ぐものは案内の一行に出ている。"""
        text = pages.new_page_confirm(1, 2)
        self.assertEqual(text, "第1ページを保存して、第2ページを出します。よろしいですか?")
        self.assertNotIn("\n", text)


class GuideTests(unittest.TestCase):
    """表の上の案内(`presenters/entry.page_guide`)。**その場面で要る一言だけ**(v4.9.0)。

        説明を一気に出しすぎです / はじめから4行長文で出すと読まないやつが大半です
        埋まった→次ページで発行 引継ぎ内容 / 赤い行があった→確かめ / 発行後→戻り方を出す
    """

    def state(self, rows):
        state = presenter.empty_state()
        for r, values in rows.items():
            for family, value in values.items():
                state.set(r, family, value)
        return state

    def guide(self, rows, **kwargs):
        kwargs = {"page": 1, "page_count": 1, "recall": False, **kwargs}
        return presenter.page_guide(self.state(rows), **kwargs)

    def assertOneLine(self, g) -> None:
        self.assertTrue(g["text"])
        self.assertNotIn("\n", g["text"])
        self.assertLessEqual(len(g["text"]), 60, g["text"])
        self.assertNotIn("steps", g, "手順を並べない")

    def test_打っている途中は何も出さない(self) -> None:
        g = self.guide({1: DONE})
        self.assertEqual(g["mode"], presenter.GUIDE_FILLING)
        self.assertNotIn(g["mode"], presenter.GUIDE_SHOWN)
        self.assertEqual(g["rows_text"], "1/12行使用(あと11行)")
        self.assertFalse(g["ready"])
        self.assertIn("12行目まで打ってから", g["reason"])

    def test_写っただけの12行目では埋まったと言わない(self) -> None:
        g = self.guide(full_rows(last={"KZ": "08", "KH": "15"}))
        self.assertEqual(g["rows_text"], "11/12行使用(あと1行)")
        self.assertFalse(g["ready"])

    def test_埋まったら次ページ発行と引き継ぐもの(self) -> None:
        g = self.guide(full_rows())
        self.assertEqual(g["mode"], presenter.GUIDE_READY)
        self.assertTrue(g["ready"])
        self.assertOneLine(g)
        self.assertIn("「次ページ発行」で第2ページへ", g["text"])
        self.assertIn("作業者・昼稼働は引き継ぎます", g["text"])
        self.assertEqual(g["action"], "goto-new-page")
        self.assertEqual(g["rows_text"], "12/12行使用")
        # 戻り方は**まだ言わない**(出したあとに言う)
        self.assertNotIn("前のページ", g["text"])

    def test_赤い行があれば先にそれを言う(self) -> None:
        g = self.guide(full_rows(), bad_rows=2)
        self.assertEqual(g["mode"], presenter.GUIDE_FIX)
        self.assertFalse(g["ready"], "赤い行があるあいだは出さない")
        self.assertOneLine(g)
        self.assertIn("赤い行(2行)を直して", g["text"])
        self.assertIn("赤い行が2行", g["reason"])
        self.assertEqual(g["action"], "")

    def test_直すところで止まったら先にそれを言う(self) -> None:
        g = presenter.guide_after_findings(self.guide(full_rows()), 3)
        self.assertEqual(g["mode"], presenter.GUIDE_FIX)
        self.assertFalse(g["ready"])
        self.assertIn("直すところ(3件)", g["text"])

    def test_出した直後は戻り方を言う(self) -> None:
        g = self.guide({}, page=2, page_count=2)
        self.assertEqual(g["mode"], presenter.GUIDE_ISSUED)
        self.assertOneLine(g)
        self.assertIn("前のページを直すときは", g["text"])
        self.assertIn("「ページ」", g["text"])
        self.assertEqual(g["action"], "goto-pages")

    def test_打ち始めたら戻り方は消える(self) -> None:
        g = self.guide({1: DONE}, page=2, page_count=2)
        self.assertEqual(g["mode"], presenter.GUIDE_FILLING)

    def test_1ページ目が空でも戻り方は言わない(self) -> None:
        """戻る先が無い。"""
        self.assertEqual(self.guide({})["mode"], presenter.GUIDE_FILLING)

    def test_確かめは1回にまとめる(self) -> None:
        """空いた行・4ページ目・何をするか、を1つの確かめに。"""
        rows = full_rows()
        rows[5] = {}
        g = self.guide(rows, page=3, page_count=3)
        self.assertIn("5行目が空のまま", g["confirm"])
        self.assertIn("すでに3ページ", g["confirm"])
        self.assertIn("第3ページを保存して、第4ページを出します", g["confirm"])

    def test_前のページを直しているあいだ(self) -> None:
        g = self.guide(full_rows(), page=1, page_count=3, recall=True)
        self.assertEqual(g["mode"], presenter.GUIDE_EDITING)
        self.assertFalse(g["ready"], "前のページからは次のページを出さない")
        self.assertOneLine(g)
        self.assertIn("第1ページを直しています", g["text"])
        self.assertIn("「保存(確定)」", g["text"])
        self.assertIn("「最新のページに戻る」", g["text"])
        self.assertEqual(g["action"], "back")
        self.assertIn("第3ページへ", g["detail"], "くわしくはマウスで")
        self.assertIn("「最新のページに戻る」", g["reason"])

    def test_他の直や見るだけのときは案内を出さない(self) -> None:
        for kwargs in ({"recall": True, "recall_other": True},
                       {"recall": False, "read_only": True}):
            with self.subTest(**kwargs):
                g = self.guide(full_rows(), **kwargs)
                self.assertEqual(g["mode"], presenter.GUIDE_NONE)
                self.assertFalse(g["ready"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    def body(self, rows, **extra) -> dict:
        return {"rows": {str(r): v for r, v in rows.items()},
                "header": {"worker": "青木"}, "checks": {}, **extra}

    def now(self):
        from nippou import work_context

        from app.routes.entry import build_service, current_calculator

        ctx = work_context.get_context()
        with self.app.test_request_context():
            service = build_service(ctx, current_calculator())
            shift, day = service.current_shift_info(datetime.now(), ctx.force_day_shift())
        return day, ctx.line, shift

    def test_写っただけの開始時刻では次のページを出さない(self) -> None:
        res = self.post("/api/entry/newpage",
                        self.body(full_rows(last={"KZ": "08", "KH": "15"})))
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "page_not_full")
        self.assertIn("開始時刻だけ", body["error"]["message"])
        self.assertEqual(body["used_rows"], 11)

    def test_12行目の終了がまだなら出さない(self) -> None:
        res = self.post("/api/entry/newpage",
                        self.body(full_rows(last={"LOT": "N7112T0"})))
        self.assertEqual(res.status_code, 422)
        self.assertIn("終了時刻がまだ", res.get_json()["error"]["message"])

    def test_空いた行があれば聞いてから出す(self) -> None:
        rows = full_rows()
        rows[4] = {}
        res = self.post("/api/entry/newpage", self.body(rows))
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertTrue(body["needs_confirm"])
        self.assertEqual(body["error"]["code"], "page_gaps")
        self.assertEqual(body["gaps"], [4])
        res = self.post("/api/entry/newpage", self.body(rows, confirm=True))
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_出したら新しいページの上に戻り方が出る(self) -> None:
        res = self.post("/api/entry/newpage", self.body(full_rows()))
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["message"], "第1ページを保存し、第2ページを出しました")
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="page-guide" data-mode="issued"', html)
        self.assertIn("前のページを直すときは", html)

    def test_赤い行があれば次のページを出さない(self) -> None:
        """「保存(確定)」は前から断っていたが、発行は素通りだった(v4.9.0)。"""
        rows = full_rows()
        rows[3] = dict(DONE, LOT="N7103T0", KH="75")   # 分が 0〜59 でない → 赤い行
        view = self.post("/api/entry/state", self.body(rows)).get_json()
        self.assertTrue(view["bad_rows"])
        self.assertEqual(view["page_guide"]["mode"], "fix")
        res = self.post("/api/entry/newpage", self.body(rows, confirm=True))
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertEqual(body["error"]["code"], "bad_rows")
        self.assertEqual(body["page_guide"]["mode"], "fix")
        day, line, shift = self.now()
        self.assertEqual(self.repo().saved_pages(day, line, shift), [])

    def test_応答に案内が付く(self) -> None:
        view = self.post("/api/entry/state", self.body(full_rows())).get_json()
        self.assertTrue(view["sheet_full"])
        self.assertEqual(view["page_guide"]["mode"], "ready")
        self.assertIn("12行目まで埋まりました", view["page_guide"]["text"])

    def test_前のページへ移ると_ボタンの名前どおりに言う(self) -> None:
        """「作業に戻る」と言っていたが、画面のボタンは「最新のページに戻る」。"""
        self.post("/api/entry/newpage", self.body(full_rows()))
        res = self.post("/api/settings/page", {"page": 1})
        self.assertEqual(res.status_code, 200, res.get_json())
        message = res.get_json()["message"]
        self.assertEqual(message, "第1ページを開きました")
        self.assertNotIn("作業に戻る", message)
        html = self.get("/").get_data(as_text=True)
        # 見出し・帯・案内の箱が「同じ直のページを直している」と言う
        self.assertIn("この直の第1ページを直しています", html)
        self.assertIn("第1ページを直し中", html)
        self.assertNotIn("過去データ " + self.now()[2], html)
        self.assertIn('id="page-guide" data-mode="editing"', html)
        self.assertIn("第1ページを直しています ─ 直したら「保存(確定)」、続きは「最新のページに戻る」",
                      html)

    def test_画面に案内の箱と戻り方の説明(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="page-guide"', html)
        self.assertIn('data-goto="new-page"', html)
        self.assertIn('data-goto="page-switch"', html)
        self.assertIn('id="page-guide-back"', html)
        self.assertNotIn("page-guide__steps", html, "手順を並べない")
        self.assertIn("12行目を終了まで打つと出せます", html)
        js = (ROOT / "app" / "static" / "js" / "views" / "entry.js").read_text(encoding="utf-8")
        for needle in ("function paintPageGuide", "guide?.confirm",
                       'getElementById("page-guide-back")'):
            with self.subTest(needle=needle):
                self.assertIn(needle, js)

    def test_ページが2つ以上なら戻り方の説明(self) -> None:
        self.post("/api/entry/newpage", self.body(full_rows()))
        html = self.get("/").get_data(as_text=True)
        self.assertIn('id="page-switch-tip"', html)
        self.assertIn("前のページを直すときは、ここで番号を選びます", html)


if __name__ == "__main__":
    unittest.main()
