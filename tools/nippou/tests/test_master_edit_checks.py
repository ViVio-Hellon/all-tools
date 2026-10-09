"""マスタ管理で 足す / 直す / 消す ── **確かめて見つかった3つ**

    マスタ管理で追加削除編集ができるのかチェックしてください

本物のマスタの写しで通して確かめたところ、足す・直す・消すはどれも
元のファイルに届き、停止理由・GW の資材重量・作業者の名簿にはすぐ
効いていました。効いていなかったのは次の3つです。

    1. 空のまま「1行足す」を押すと、中身の無い行が共有のマスタに残る
    2. 時間用を直しても、起動し直すか「時間マスタを取り込む」を押すまで
       直の時間に効かない(直の時間だけは手元のDBへ写したものを見ている)
    3. 時間用の 開始・終了 は形を見ていない。「7時」「25:00」が通ると、
       直の時刻を読む画面がぜんぶ例外で止まる ── 起動のたびにマスタを
       読み直すので、起動し直しても逃げられない
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


class _Transmission(WebTestCase):
    """伝送用ファイルを置き、編集の鍵を開けた状態から始める。"""

    #: 現場の 時間用 と同じ形(型の無い列・直は 1/2/3/昼)
    SHIFT_ROWS = [(1, "1", "07:00", "15:00"), (2, "2", "15:00", "22:50"),
                  (3, "3", "22:50", "07:00"), (4, "昼", "08:15", "17:00")]

    def setUp(self) -> None:
        super().setUp()
        self.ref = self.tmp / "ref"
        self.ref.mkdir(exist_ok=True)
        self.path = self.ref / "伝送用ファイル.sqlite3"
        conn = sqlite3.connect(str(self.path))
        with conn:
            conn.execute('CREATE TABLE "時間用" ("番号", "直", "開始", "終了", "更新日")')
            conn.executemany('INSERT INTO "時間用" VALUES (?,?,?,?,"")',
                             self.SHIFT_ROWS)
            conn.execute('CREATE TABLE "作業停止時間内訳_1" '
                         '("管理番号" INTEGER, "内訳" TEXT, "内訳番号" TEXT)')
            conn.execute('INSERT INTO "作業停止時間内訳_1" VALUES (1, "休憩食事", "0")')
        conn.close()
        self.post("/api/settings/paths",
                  {"gw_reference_dir": str(self.ref), "password": "nisk"})
        self.post("/api/master/unlock", {"enable": True, "password": "nisk"})

    def count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.path))
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()

    def shift_rows(self) -> dict[str, tuple[str, str]]:
        conn = sqlite3.connect(str(self.path))
        try:
            return {r[0]: (r[1], r[2]) for r in conn.execute(
                'SELECT "直", "開始", "終了" FROM "時間用" ORDER BY rowid')}
        finally:
            conn.close()

    def rowid_of(self, key: str) -> int:
        conn = sqlite3.connect(str(self.path))
        try:
            return conn.execute('SELECT rowid FROM "時間用" WHERE "直" = ?',
                                (key,)).fetchone()[0]
        finally:
            conn.close()

    def save(self, key: str, values: dict):
        return self.post("/api/master/row/save", {
            "file": "transmission", "table": "時間用",
            "key": self.rowid_of(key), "values": values})

    def add(self, table: str, values: dict):
        return self.post("/api/master/row/add",
                         {"file": "transmission", "table": table, "values": values})

    def local(self) -> dict[str, tuple[str, str]]:
        return self.repo().get_shift_times()


class EmptyRowTests(_Transmission):
    """1. **空のまま「1行足す」を押しても、行は増えない。**"""

    def test_全部空なら断る(self) -> None:
        before = self.count("作業停止時間内訳_1")
        res = self.add("作業停止時間内訳_1",
                       {"管理番号": "", "内訳": " ", "内訳番号": ""})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["message"], "入れる値がありません。")
        self.assertEqual(self.count("作業停止時間内訳_1"), before)

    def test_何も送らなくても断る(self) -> None:
        before = self.count("作業停止時間内訳_1")
        res = self.add("作業停止時間内訳_1", {})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.count("作業停止時間内訳_1"), before)

    def test_1つでも入っていれば足す(self) -> None:
        res = self.add("作業停止時間内訳_1", {"内訳": "段取り"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.count("作業停止時間内訳_1"), 2)


class ShiftFormTests(_Transmission):
    """3. 時間用の 開始・終了 は **時:分** でなければ書かない。"""

    def test_形の違う時刻は断る(self) -> None:
        for bad in ("7時", "25:00", "07:60", "0700", "あ"):
            with self.subTest(bad=bad):
                res = self.save("1", {"開始": bad})
                self.assertEqual(res.status_code, 400)
                self.assertIn("時:分", res.get_json()["error"]["message"])
        self.assertEqual(self.shift_rows()["1"], ("07:00", "15:00"))

    def test_空の時刻も断る(self) -> None:
        """空にすると、その直は黙って控えの時刻で動きます。"""
        res = self.save("2", {"終了": ""})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.shift_rows()["2"], ("15:00", "22:50"))

    def test_書き方の揺れは揃えて書く(self) -> None:
        """全角・1桁の時は、現場の書き方(07:00)に揃えて書く。"""
        res = self.save("1", {"開始": "６：５５", "終了": "15:05:00"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.shift_rows()["1"], ("06:55", "15:05"))

    def test_知らない直は断る(self) -> None:
        res = self.add("時間用", {"番号": "5", "直": "4",
                                "開始": "07:00", "終了": "15:00"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("1・2・3・昼", res.get_json()["error"]["message"])

    def test_同じ直を2行にしない(self) -> None:
        """読む側は後の行で上書きするので、前の行を直しても効かなくなる。"""
        res = self.add("時間用", {"番号": "5", "直": "１",
                                "開始": "06:00", "終了": "14:00"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("もうあります", res.get_json()["error"]["message"])
        self.assertEqual(self.count("時間用"), 4)

    def test_自分の行はそのまま直せる(self) -> None:
        """直を送り直しても(画面は行の全部の列を送る)、自分とは重ならない。"""
        res = self.save("3", {"直": "3", "開始": "22:45", "終了": "07:00"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_直を他の直に付け替えるのは断る(self) -> None:
        res = self.save("3", {"直": "1"})
        self.assertEqual(res.status_code, 400)

    def test_ほかの表は形を見ない(self) -> None:
        """決まりは時間用だけ。停止理由の表に「7時」と書くのは自由。"""
        res = self.add("作業停止時間内訳_1", {"内訳": "7時"})
        self.assertEqual(res.status_code, 200)


class ShiftGapTests(_Transmission):
    """**時間マスタは隙間なく登録させる**(現場: 隙間は無い。重なりは構わない)。

    隙間があると、隙間の時刻の「直の終わり」を催促・自動確定と画面の固定とで別に決めてしまう
    (tests/test_shift_end_agree.py の KNOWN_GAPS)。登録の時点で作らせない。
    """

    def test_隙間ができる直しは断る(self) -> None:
        res = self.save("1", {"直": "1", "開始": "07:00", "終了": "14:30"})
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertIn("隙間", res.get_json()["error"]["message"])
        self.assertEqual(self.shift_rows()["1"], ("07:00", "15:00"), "断ったのに書いた")

    def test_日をまたぐ隙間も断る(self) -> None:
        res = self.save("3", {"直": "3", "開始": "22:50", "終了": "06:30"})
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertIn("3直の終わり 06:30 と 1直の始まり 07:00", res.get_json()["error"]["message"])

    def test_重なりは構わない(self) -> None:
        res = self.save("2", {"直": "2", "開始": "14:50", "終了": "22:50"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_日勤は並びに入らない(self) -> None:
        res = self.save("昼", {"直": "昼", "開始": "09:00", "終了": "16:00"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def test_隙間のある時間マスタは帯で知らせる(self) -> None:
        from nippou.logic import shift

        note = shift.shift_times_note({"1": ("07:00", "15:00"), "2": ("15:30", "22:50"),
                                       "3": ("22:50", "07:00")})
        self.assertIn("隙間", note)
        self.assertIn("1直の終わり 15:00 と 2直の始まり 15:30", note)
        self.assertEqual(shift.shift_times_note({"1": ("06:50", "15:00"), "2": ("14:50", "22:50"),
                                                 "3": ("22:50", "07:00")}), "")


class ShiftTakesEffectTests(_Transmission):
    """2. 時間用を直したら、**直の時間にすぐ効く。**"""

    def setUp(self) -> None:
        super().setUp()
        self.post("/api/settings/sync-shift", {})       # 起動時に読んだのと同じ

    def test_直すとすぐ手元に写る(self) -> None:
        """**ここが本題。** 以前は起動し直すまで 07:00 のままでした。"""
        res = self.save("1", {"開始": "06:55"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.local()["1"], ("06:55", "15:00"))
        message = res.get_json()["message"]
        self.assertIn("直の時間にもすぐ効かせました", message)
        self.assertIn("1直 06:55〜15:00", message)

    def test_同じ画面の直の境界時刻も描き直せる(self) -> None:
        """応答に写した値が載る。画面はそれで「直の境界時刻」の表を描き直す。"""
        body = self.save("1", {"開始": "06:55"}).get_json()
        self.assertIn(["1", "06:55", "15:00"], body["shift_times"])
        html = self.get("/settings?tab=master").get_data(as_text=True)
        self.assertIn('id="shift-times-body"', html)
        from nippou.logic.shift import default_times_text
        self.assertIn(default_times_text(), html)
        self.assertNotIn("1直 08:00-17:00", html)
        script = (Path(__file__).resolve().parent.parent
                  / "app/static/js/views/master.js").read_text(encoding="utf-8")
        self.assertIn("paintShiftTimes(body.shift_times)", script)

    def test_ほかの表では直の時刻を返さない(self) -> None:
        body = self.add("作業停止時間内訳_1", {"内訳": "段取り"}).get_json()
        self.assertNotIn("shift_times", body)

    def test_直の判定にも効く(self) -> None:
        self.save("1", {"開始": "06:30"})
        from app.routes.entry import build_shift_calculator
        calc = build_shift_calculator(self.local())     # 画面が使うのと同じ組み立て
        self.assertEqual(calc.time_check(datetime(2026, 9, 24, 6, 45)), "1直")

    def test_消した直は手元からも消える(self) -> None:
        """消したのに効き続ける、にしない(手元をマスタに揃える)。"""
        self.assertIn("昼", self.local())
        res = self.post("/api/master/row/delete", {
            "file": "transmission", "table": "時間用", "key": self.rowid_of("昼")})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertNotIn("昼", self.local())

    def test_足した直も写る(self) -> None:
        self.post("/api/master/row/delete", {
            "file": "transmission", "table": "時間用", "key": self.rowid_of("昼")})
        res = self.add("時間用", {"番号": "4", "直": "昼",
                                "開始": "08:30", "終了": "17:15"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.local()["昼"], ("08:30", "17:15"))

    def test_写せなくても書いたことは残す(self) -> None:
        """元のファイルにはもう書けている。**押す場所を言う。**"""
        with patch("nippou.access_bridge.importer.import_shift_times",
                   return_value=None):
            res = self.save("1", {"開始": "06:50"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("時間マスタを取り込む", res.get_json()["message"])
        self.assertEqual(self.shift_rows()["1"], ("06:50", "15:00"))

    def test_写す途中で例外でも書いたことは残す(self) -> None:
        with patch("nippou.services.shift_times.reload",
                   side_effect=RuntimeError("想定外")):
            res = self.save("1", {"開始": "06:50"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("まだ効いていません", res.get_json()["message"])

    def test_ほかの表を直しても直の時間は読み直さない(self) -> None:
        with patch("nippou.services.shift_times.reload") as reload:
            self.add("作業停止時間内訳_1", {"内訳": "段取り"})
        reload.assert_not_called()

    def test_断ったら読み直さない(self) -> None:
        with patch("nippou.services.shift_times.reload") as reload:
            self.save("1", {"開始": "7時"})
        reload.assert_not_called()


class ReadSideTests(unittest.TestCase):
    """読む側の最後の砦 ── **形の違う値が入っていても画面は落ちない。**

    マスタ管理を通さずに入ることがあります(上流が書く・別の道具で直す)。
    """

    @unittest.skipUnless(HAS_FLASK, SKIP_REASON)
    def test_形の違う時刻は控えに落ちる(self) -> None:
        from app.routes.entry import build_shift_calculator
        from nippou.logic.shift import DEFAULT_SHIFT_TIMES
        for bad in (("7時", "15:00"), ("25:00", "15:00"), ("07:00", "")):
            with self.subTest(bad=bad):
                calc = build_shift_calculator({"1": bad})
                self.assertEqual(calc.times.start1, DEFAULT_SHIFT_TIMES.start1)
                calc.time_check(datetime(2026, 9, 24, 10, 0))      # 例外が出ない

    @unittest.skipUnless(HAS_FLASK, SKIP_REASON)
    def test_揺れた書き方は読む(self) -> None:
        from app.routes.entry import build_shift_calculator
        calc = build_shift_calculator({"1": ("6:55", "１５：００"),
                                       "2": ("15:00:00", "22:50:00")})
        self.assertEqual((calc.times.start1, calc.times.end1), ("06:55", "15:00"))
        self.assertEqual((calc.times.start2, calc.times.end2), ("15:00", "22:50"))

    def test_保存前チェックも同じ決め方(self) -> None:
        """`bounds_of` と `build_shift_calculator` が同じ直を別の時刻で見ない。"""
        from nippou.services.shift_check import bounds_of
        self.assertEqual(bounds_of({"1": ("7時", "15:00")}, "1直"), ("07:00", "15:00"))
        self.assertEqual(bounds_of({"1": ("6:55", "15:00")}, "1直"), ("06:55", "15:00"))

    def test_帯は直す場所を言う(self) -> None:
        """形が違うなら、取り込み直しても同じ値が来るだけ。"""
        from nippou.logic import shift
        raw = {"1": ("7時", "15:00"), "2": ("15:00", "22:50"),
               "3": ("22:50", "07:00")}
        self.assertEqual(shift.missing_shift_times(raw), ["1直"])
        self.assertEqual(shift.malformed_shift_times(raw), ["1直"])
        note = shift.shift_times_note(raw)
        self.assertIn("時間用", note)
        self.assertNotIn("取り込む", note)

    def test_日勤の形違いも言う(self) -> None:
        """日勤は無くても普通。**入っているのに読めない**のは打ち間違い。"""
        from nippou.logic import shift
        full = {"1": ("07:00", "15:00"), "2": ("15:00", "22:50"),
                "3": ("22:50", "07:00")}
        self.assertEqual(shift.shift_times_note(full), "")
        self.assertIn("日勤", shift.shift_times_note({**full, "昼": ("8時", "17:00")}))

    def test_控えの時刻は控えから書く(self) -> None:
        """画面の文が、控えを直したあとも古い時刻を言い続けないように。"""
        from nippou.logic.shift import DEFAULT_SHIFT_TIMES, default_times_text
        text = default_times_text()
        self.assertIn(f"1直 {DEFAULT_SHIFT_TIMES.start1}-{DEFAULT_SHIFT_TIMES.end1}", text)
        self.assertIn(f"日勤 {DEFAULT_SHIFT_TIMES.start_day}-", text)

    def test_作業時間の上限も落ちない(self) -> None:
        from nippou.logic.work_time import shift_minutes
        self.assertEqual(shift_minutes("7時", "15:00"), 0)
        self.assertEqual(shift_minutes("07:00", "15:00"), 480)
        self.assertEqual(shift_minutes("22:50", "07:00"), 490)

    def test_読み方(self) -> None:
        from nippou.logic.shift import normalize_hhmm
        cases = {"7:00": "07:00", "07:00": "07:00", "０７：００": "07:00",
                 "23:59": "23:59", "7:00:00": "07:00", " 8:15 ": "08:15",
                 "24:00": "", "7時": "", "": "", None: "", "7:0": "", "0700": ""}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_hhmm(raw), expected)


class SyncMirrorTests(WebTestCase):
    """写し方は3か所で同じ ── **マスタに揃える。**"""

    def test_取り込むとマスタに無い直は消える(self) -> None:
        self.repo().set_shift_time("昼", "08:00", "17:00")
        with patch("nippou.access_bridge.importer.import_shift_times",
                   return_value={"1": ("07:00", "15:00")}):
            res = self.post("/api/settings/sync-shift", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.repo().get_shift_times(), {"1": ("07:00", "15:00")})

    def test_読めなければ手元に触らない(self) -> None:
        self.repo().set_shift_time("1", "07:00", "15:00")
        for empty in (None, {}):
            with self.subTest(empty=empty), patch(
                    "nippou.access_bridge.importer.import_shift_times",
                    return_value=empty):
                res = self.post("/api/settings/sync-shift", {})
            self.assertEqual(res.status_code, 502)
            self.assertEqual(self.repo().get_shift_times(), {"1": ("07:00", "15:00")})

    def test_起動も同じ道を通る(self) -> None:
        source = Path(__file__).resolve().parent.parent / "start_app.py"
        text = source.read_text(encoding="utf-8")
        body = text[text.index("def _read_shift_times"):text.index("def _watch_for_idle")]
        self.assertIn("shift_times.reload(", body)
        self.assertNotIn("set_shift_time(", body)


if __name__ == "__main__":
    unittest.main()
