"""入力の近道 (v4.7.0)

    入力が短縮されるような便利機能は何かないですかね？
    → 全部入れてください / マウスムーブで簡易説明が出るようにしてください

実物の日報(13ファイル・40ページ)で数えた「人が毎回打っているもの」を
減らす6つと、欄にマウスを乗せると出る簡易説明:

    1 枚数・包数のダブルクリック → 上の行と同じ
    2 Enter / Shift+Enter で次の欄・前の欄
    3 包数が空のまま離れる → 1
    4 停止記号の一覧の上に「よく使う」
    5 終了の時/分に「直の終わり」
    6 作業者: 班でまとめて選ぶ・いまの名前に最初からチェック

どれも**押した・離れたときだけ**入る(黙って先回りしない)ことも見ます。
本物のブラウザでの確かめは README の v4.7.0 にあります。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import constants  # noqa: E402
from nippou.logic import input_shortcuts as sc  # noqa: E402
from nippou.logic import staff  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# 1. 上の行と同じ
# ----------------------------------------------------------------------
class CopyAboveTests(unittest.TestCase):
    def test_真上の行から写す(self) -> None:
        got = sc.copy_above({1: {"MAI": "2", "TUT": "1"}, 2: {}}, 2)
        self.assertTrue(got.ok)
        self.assertEqual((got.from_row, got.values), (1, {"MAI": "2", "TUT": "1"}))
        self.assertIn("1行目と同じ", got.message)

    def test_枚数の無い行は飛ばして上へさかのぼる(self) -> None:
        """真上が停止だけの行(全停など)のことがある。"""
        rows = {1: {"MAI": "8", "TUT": "1"}, 2: {"S": "0", "TH": "60"}, 3: {}}
        got = sc.copy_above(rows, 3)
        self.assertEqual((got.ok, got.from_row, got.values["MAI"]), (True, 1, "8"))

    def test_包数だけでも写す(self) -> None:
        got = sc.copy_above({1: {"TUT": "1"}}, 2)
        self.assertEqual(got.values, {"MAI": "", "TUT": "1"})

    def test_1行目は上に行が無い(self) -> None:
        got = sc.copy_above({1: {"MAI": "2"}}, 1)
        self.assertFalse(got.ok)
        self.assertIn("上に行がありません", got.message)

    def test_上に枚数の入った行が無ければ断る(self) -> None:
        got = sc.copy_above({1: {"LOT": "N7131T0"}, 2: {}}, 3)
        self.assertFalse(got.ok)
        self.assertEqual(got.values, {})

    def test_行の外は断る(self) -> None:
        for row in (0, constants.ROW_COUNT + 1, -3):
            with self.subTest(row=row):
                self.assertFalse(sc.copy_above({1: {"MAI": "2"}}, row).ok)


# ----------------------------------------------------------------------
# 3. 包数が空なら 1
# ----------------------------------------------------------------------
class DefaultPacksTests(unittest.TestCase):
    def test_枚数があって包数が空なら1(self) -> None:
        for changed in ("MAI", "TUT"):
            with self.subTest(changed=changed):
                self.assertEqual(sc.default_packs({"MAI": "2", "TUT": ""}, changed), "1")

    def test_ほかの欄を離れたときは入れない(self) -> None:
        self.assertIsNone(sc.default_packs({"MAI": "2", "TUT": ""}, "SH"))
        self.assertIsNone(sc.default_packs({"MAI": "2", "TUT": ""}, ""))

    def test_枚数が空の行には入れない(self) -> None:
        """停止だけの行に 1 だけ残らないように。"""
        self.assertIsNone(sc.default_packs({"MAI": "", "TUT": ""}, "MAI"))

    def test_打った包数は触らない(self) -> None:
        self.assertIsNone(sc.default_packs({"MAI": "2", "TUT": "3"}, "TUT"))


# ----------------------------------------------------------------------
# 4. よく使う
# ----------------------------------------------------------------------
class FrequentTests(unittest.TestCase):
    AVAILABLE = {"レ", "0", "G", "イ", "B", "C"}

    def test_多い順(self) -> None:
        used = ["G", "レ", "0", "レ", "0", "レ", "G", "0", "レ"]
        self.assertEqual(sc.frequent_codes(used, self.AVAILABLE), ["レ", "0", "G"])

    def test_1回きりは並べない(self) -> None:
        self.assertEqual(sc.frequent_codes(["レ", "レ", "イ"], self.AVAILABLE), ["レ"])

    def test_一覧に無い記号は並べない(self) -> None:
        """選べない記号を出しても押せない(マスタから消えたもの)。"""
        self.assertEqual(sc.frequent_codes(["ヌ", "ヌ", "ヌ", "B", "B"], self.AVAILABLE), ["B"])

    def test_同じ回数なら先に出たほう(self) -> None:
        """新しい保存から読んでいるので、より最近のほうが上。"""
        self.assertEqual(sc.frequent_codes(["C", "B", "B", "C"], self.AVAILABLE), ["C", "B"])

    def test_数は上限まで(self) -> None:
        used = [c for c in "レ0GイBC" for _ in range(3)]
        self.assertEqual(len(sc.frequent_codes(used, self.AVAILABLE, limit=4)), 4)
        self.assertEqual(sc.FREQUENT_LIMIT, 7)

    def test_空(self) -> None:
        self.assertEqual(sc.frequent_codes([], self.AVAILABLE), [])
        self.assertEqual(sc.frequent_codes(["", " ", None], self.AVAILABLE), [])


# ----------------------------------------------------------------------
# 5. 直の終わり
# ----------------------------------------------------------------------
class ShiftEndTests(unittest.TestCase):
    ENDS = {"1直": ("15", "00"), "2直": ("22", "50"), "3直": ("7", "0")}

    def test_その直の終わり(self) -> None:
        self.assertEqual(sc.shift_end("1直", self.ENDS), ("15", "00"))
        self.assertEqual(sc.shift_end("2直", self.ENDS), ("22", "50"))

    def test_2桁にそろえる(self) -> None:
        self.assertEqual(sc.shift_end("3直", self.ENDS), ("07", "00"))

    def test_分からなければ入れない(self) -> None:
        self.assertIsNone(sc.shift_end("日勤", self.ENDS))
        self.assertIsNone(sc.shift_end("1直", {"1直": ("", "")}))


# ----------------------------------------------------------------------
# 2. Enter
# ----------------------------------------------------------------------
class EnterTests(unittest.TestCase):
    ORDER = ["LOT", "ZAI", "SIZ", "KEN", "KZ", "KH", "SZ", "SH", "HIT", "AI", "MAI", "TUT",
             "VC", "ET", "S", "TH", "SS", "THS", "STH", "THT", "S4", "TH4", "S5", "TH5",
             "CON", "WEI", "UNI"]

    def setUp(self) -> None:
        self.t = sc.enter_targets(self.ORDER)

    def test_打つ順に進む(self) -> None:
        """2桁で自動で進むのと同じ道(`FOCUS_CHAIN`)。"""
        self.assertEqual((self.t["ZAI"].next_family, self.t["ZAI"].next_row), ("SIZ", 0))
        self.assertEqual((self.t["SIZ"].prev_family, self.t["SIZ"].prev_row), ("ZAI", 0))
        self.assertEqual(self.t["HIT"].next_family, "MAI")       # 合紙は打つ順に無い
        self.assertEqual(self.t["ET"].next_family, "S")

    def test_行の最後の次は次の行のロット(self) -> None:
        self.assertEqual((self.t["THT"].next_family, self.t["THT"].next_row), ("S4", 0))
        self.assertEqual((self.t["TH5"].next_family, self.t["TH5"].next_row), ("LOT", 1))
        self.assertEqual((self.t["LOT"].prev_family, self.t["LOT"].prev_row), ("TH5", -1))

    def test_打つ順に無い欄は右の次の欄へ(self) -> None:
        self.assertEqual((self.t["AI"].next_family, self.t["AI"].prev_family), ("MAI", "HIT"))
        for family in ("CON", "WEI", "UNI"):
            with self.subTest(family=family):
                self.assertEqual((self.t[family].next_family, self.t[family].next_row),
                                 ("LOT", 1))
                self.assertEqual(self.t[family].prev_family, "TH5")

    def test_画面の列ぜんぶに行き先がある(self) -> None:
        from nippou.presenters import entry as presenter

        editable = [c.family for c in presenter.COLUMNS if c.kind != "calc"]
        targets = sc.enter_targets(editable)
        self.assertEqual(set(targets), set(editable))
        for t in targets.values():
            self.assertIn(t.next_family, editable)
            self.assertIn(t.prev_family, editable)

    def test_属性の形(self) -> None:
        attrs = sc.enter_attributes(self.t["TH5"])
        self.assertEqual(attrs, {"data-enter-next": "LOT", "data-enter-next-row": "1",
                                 "data-enter-prev": "S5", "data-enter-prev-row": "0"})
        self.assertEqual(sc.enter_attributes(None), {})


# ----------------------------------------------------------------------
# 簡易説明
# ----------------------------------------------------------------------
class HintTests(unittest.TestCase):
    def test_画面の列ぜんぶに説明がある(self) -> None:
        from nippou.presenters import entry as presenter

        for column in presenter.COLUMNS:
            with self.subTest(family=column.family):
                self.assertTrue(sc.hint(column.family))

    def test_近道は説明に書いてある(self) -> None:
        self.assertIn("上の行と同じ", sc.hint("MAI"))
        self.assertIn("1 が入ります", sc.hint("TUT"))
        self.assertIn("直の終わり", sc.hint("SZ"))
        self.assertIn("よく使う", sc.hint("S"))
        self.assertIn("7桁", sc.hint("LOT"))

    def test_短く言い切る(self) -> None:
        """マウスを乗せて読むもの。長い説明は画面の「くわしく」に。"""
        for family, text in sc.HINTS.items():
            with self.subTest(family=family):
                self.assertLessEqual(len(text), 80)

    def test_知らない欄は空(self) -> None:
        self.assertEqual(sc.hint("NOPE"), "")


# ----------------------------------------------------------------------
# 6. 作業者
# ----------------------------------------------------------------------
class StaffTests(unittest.TestCase):
    ROSTER = ["青木", "井上", "江藤"]

    def test_空白は半角でも全角でも分ける(self) -> None:
        self.assertEqual(staff.split_worker("青木　井上 江藤"), ["青木", "井上", "江藤"])
        self.assertEqual(staff.split_worker(""), [])

    def test_いま居る名簿の人に最初からチェック(self) -> None:
        self.assertEqual(staff.preselect("新人教育 江藤 青木", self.ROSTER), ["江藤", "青木"])
        self.assertEqual(staff.preselect("", self.ROSTER), [])

    def test_名簿に無い字は消さない(self) -> None:
        got = staff.merge_worker("新人教育 青木", ["青木", "井上"], self.ROSTER)
        self.assertEqual(got, "新人教育 青木 井上")

    def test_チェックを外した名簿の人は消える(self) -> None:
        got = staff.merge_worker("青木 井上", ["井上"], self.ROSTER)
        self.assertEqual(got, "井上")

    def test_今の欄が無ければ前と同じ(self) -> None:
        self.assertEqual(staff.merge_worker("", ["青木", "江藤"], self.ROSTER), "青木 江藤")


# ----------------------------------------------------------------------
# 画面とAPI
# ----------------------------------------------------------------------
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    def state(self, rows, **extra) -> dict:
        body = {"rows": rows, "header": {"worker": "青木"}, "checks": {}, **extra}
        res = self.post("/api/entry/state", body)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True)[:300])
        return res.get_json()

    def make_stop_master(self) -> None:
        ref = self.tmp / "ref"
        ref.mkdir(exist_ok=True)
        conn = sqlite3.connect(str(ref / "伝送用ファイル.sqlite3"))
        with conn:
            for table, rows in (
                ("作業停止時間内訳_1", [("1", "休憩食事", "0"), ("2", "朝礼", "1")]),
                ("作業停止時間内訳_2", [("1", "段取り（板）", "レ"), ("2", "突発停止", "イ")]),
                ("作業停止時間内訳_3", [("1", "ビニール交換", "G")]),
            ):
                conn.execute(f'CREATE TABLE "{table}" ("管理番号", "内訳", "内訳番号", "備考")')
                conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, "")', rows)
        conn.close()
        self.post("/api/settings/paths", {"gw_reference_dir": str(ref), "password": "nisk"})

    # ---- 1. 上の行と同じ ----
    def test_上の行と同じを頼むと枚数と包数が入る(self) -> None:
        body = self.state({"1": {"MAI": "2", "TUT": "1"}, "2": {"KZ": "08", "KH": "30"}},
                          row=2, changed="MAI", copy_above={"row": 2})
        self.assertTrue(body["copied"]["ok"])
        two = body["rows"]["2"]
        self.assertEqual((two["MAI"], two["TUT"], two["CON"]), ("2", "1", "2"))

    def test_上に無ければ断って何も変えない(self) -> None:
        body = self.state({"1": {"MAI": ""}}, row=1, changed="MAI", copy_above={"row": 1})
        self.assertFalse(body["copied"]["ok"])
        self.assertEqual(body["rows"]["1"]["MAI"], "")

    def test_頼まなければ写さない(self) -> None:
        body = self.state({"1": {"MAI": "2", "TUT": "1"}, "2": {}}, row=2, changed="KZ")
        self.assertNotIn("copied", body)
        self.assertEqual(body["rows"]["2"]["MAI"], "")

    # ---- 3. 包数が空なら 1 ----
    def test_枚数を離れると包数に1(self) -> None:
        body = self.state({"1": {"MAI": "2"}}, row=1, changed="MAI")
        self.assertEqual(body["rows"]["1"]["TUT"], "1")
        self.assertEqual(body["rows"]["1"]["CON"], "2")
        self.assertEqual(body["filled"], [{"row": 1, "family": "TUT", "value": "1"}])

    def test_ほかの欄を離れたときは入れない(self) -> None:
        body = self.state({"1": {"MAI": "2"}}, row=1, changed="SH")
        self.assertEqual(body["rows"]["1"]["TUT"], "")
        self.assertNotIn("filled", body)

    # ---- 5. 直の終わり ----
    def test_直の終わりを押すと終了に入り_次の行へ写らない(self) -> None:
        body = self.state({"1": {"KZ": "08", "KH": "00"}, "2": {}}, row=1, changed="SH",
                          stamp={"row": 1, "which": "shift_end"})
        self.assertTrue(body["stamp"]["ok"], body["stamp"])
        end = body["shift_end"]
        self.assertRegex(end, r"^\d\d:\d\d$")
        one = body["rows"]["1"]
        self.assertEqual(f"{one['SZ']}:{one['SH']}", end)
        self.assertEqual(body["stamp"]["text"], end)
        # 直の終わりは次の行の開始に写らない(`navigation.same_text`)
        self.assertEqual((body["rows"]["2"]["KZ"], body["rows"]["2"]["KH"]), ("", ""))

    def test_直の終わりは過去の直を開いていても入る(self) -> None:
        """いまの時刻ではなく、その直の終わりの時刻なので。"""
        from nippou import work_context
        from nippou.services.nippou_service import RecallState

        work_context.get_context().recall = RecallState(
            active=True, report_date="2026年9月30日", line="L-1", shift="2直", page=1)
        body = self.state({"1": {"KZ": "15", "KH": "00"}}, row=1, changed="SH",
                          stamp={"row": 1, "which": "shift_end"})
        self.assertTrue(body["stamp"]["ok"])
        self.assertEqual(body["stamp"]["text"], "22:50")

    # ---- 4. よく使う ----
    def test_停止記号の一覧の上によく使う(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        self.make_stop_master()
        for i, (s, ss) in enumerate([("レ", "0"), ("レ", "0"), ("レ", "G"), ("イ", "")], 1):
            key = dict(report_date=f"2026年9月{i}日", line="L-1", shift="1直", page=1)
            self.repo().save(HeaderRecord(**key, worker="青木"),
                             [DetailRecord(**key, row_no=1, s=s, th="10", ss=ss,
                                           ths="10" if ss else "")])
        html = self.get("/").get_data(as_text=True)
        top = html.split('data-frequent="1">', 1)[1].split("</optgroup>", 1)[0]
        self.assertEqual([part.split('"')[0] for part in top.split('value="')[1:]],
                         ["レ", "0"])          # 1回きり(G・イ)は並べない
        self.assertIn("よく使う(このライン)", html)

    def test_よく使うは他のラインを数えない(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        for i in range(3):
            key = dict(report_date=f"2026年9月{i + 1}日", line="HVC", shift="1直", page=1)
            self.repo().save(HeaderRecord(**key, worker="青木"),
                             [DetailRecord(**key, row_no=1, s="レ", th="10")])
        self.assertEqual(self.repo().recent_stop_codes("L-1"), [])
        self.assertEqual(self.repo().recent_stop_codes("HVC"), ["レ", "レ", "レ"])

    # ---- 画面の印 ----
    def test_画面に近道の印と簡易説明(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertIn('data-copy-above="1"', html)
        self.assertIn('data-enter-next="SIZ"', html)
        self.assertIn('id="shift-end-chip"', html)
        self.assertIn('id="shortcut-tip"', html)
        self.assertIn(f'data-hint="{sc.hint("MAI")}"', html)
        self.assertIn('id="staff-lead"', html)

    def test_画面の配線(self) -> None:
        js = (ROOT / "app" / "static" / "js" / "views" / "entry.js").read_text(encoding="utf-8")
        for needle in ("copy_above: { row }", 'which: "shift_end"', "moveByEnter(",
                       "event.isComposing", "paintFilled(view.filled)"):
            with self.subTest(needle=needle):
                self.assertIn(needle, js)
        app_js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('import { startHints } from "./hint.js"', app_js)
        hint_js = (ROOT / "app" / "static" / "js" / "hint.js").read_text(encoding="utf-8")
        self.assertIn("[data-hint]", hint_js)
        modals = (ROOT / "app" / "static" / "js" / "views" / "modals.js").read_text(encoding="utf-8")
        self.assertIn("/api/staff/preselect", modals)
        self.assertIn("data-staff-team", modals.replace("dataset.staffTeam", "data-staff-team"))

    # ---- 6. 作業者 ----
    def test_作業者_最初からチェックする名前(self) -> None:
        body = self.post("/api/staff/preselect",
                         {"current": "新人教育 江藤 青木", "roster": ["青木", "井上", "江藤"]}
                         ).get_json()
        self.assertEqual(body["checked"], ["江藤", "青木"])

    def test_作業者_反映しても名簿に無い字は残る(self) -> None:
        body = self.post("/api/staff/apply",
                         {"names": ["青木", "井上"], "current": "新人教育 江藤",
                          "roster": ["青木", "井上", "江藤"]}).get_json()
        self.assertEqual(body["worker"], "新人教育 青木 井上")

    def test_作業者_前からの呼び方も通る(self) -> None:
        """`current` と `roster` を付けない呼び方(前の画面)でも同じに動く。"""
        body = self.post("/api/staff/apply", {"names": ["青木", "井上"]}).get_json()
        self.assertEqual(body["worker"], "青木 井上")


if __name__ == "__main__":
    unittest.main()
