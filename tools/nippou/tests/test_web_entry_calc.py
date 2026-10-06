"""入力 → 計算 → 出力の順が、APIを通しても崩れないこと

VBA がシートへ直接打たせずフォームを挟んでいたのは、**入力した時点で
計算と変換を済ませてから出力するため**でした。Web版でも同じで、欄から
離れるたびに `/api/entry/state` を通り、決まった値が返ってきます。

ここが見ているのは「1つ1つの式」ではなく(それは test_work_time /
test_weight_calc)、**画面から送ったものが、その順で処理されて返るか**です。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HEADERS, WebTestCase                    # noqa: E402


class CalcThroughApiTests(WebTestCase):
    def state(self, rows: dict, header: dict | None = None) -> dict:
        res = self.post("/api/entry/state",
                        {"rows": rows, "header": header or {}})
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    # ---- 時間 --------------------------------------------------------
    def test_開始終了から作業時間が出る(self) -> None:
        body = self.state({"1": {"KZ": "22", "KH": "50", "SZ": "00", "SH": "30"}})
        self.assertEqual(body["rows"]["1"]["TIM"], "100")
        self.assertIsNone(body["time_problem"])

    def test_停止時間を引く(self) -> None:
        body = self.state({"1": {"KZ": "07", "KH": "00", "SZ": "09", "SH": "00",
                                 "TH": "20", "THS": "10"}})
        self.assertEqual(body["rows"]["1"]["TIM"], "90")

    def test_時が範囲外なら断りを返す(self) -> None:
        body = self.state({"1": {"KZ": "25", "KH": "00", "SZ": "09", "SH": "00"}})
        problem = body["time_problem"]
        self.assertIsNotNone(problem)
        self.assertEqual(problem["reason"], "time_range")
        self.assertEqual(problem["row"], 1)
        self.assertEqual(problem["field"], "KZ")
        self.assertFalse(problem["sound"])
        # **断ったら時間の欄は空のまま**(VBA も Exit Sub して書かなかった)
        self.assertEqual(body["rows"]["1"]["TIM"], "")

    def test_分が範囲外なら断る(self) -> None:
        body = self.state({"1": {"KZ": "07", "KH": "70", "SZ": "09", "SH": "00"}})
        self.assertEqual(body["time_problem"]["field"], "KH")

    def test_マイナスなら音を鳴らす合図を返す(self) -> None:
        body = self.state({"1": {"KZ": "07", "KH": "00", "SZ": "08", "SH": "00",
                                 "TH": "90"}})
        problem = body["time_problem"]
        self.assertEqual(problem["reason"], "negative_time")
        self.assertTrue(problem["sound"])
        self.assertIn("マイナス", problem["message"])

    def test_同時刻なら断る(self) -> None:
        body = self.state({"2": {"KZ": "07", "KH": "00", "SZ": "07", "SH": "00"}})
        self.assertEqual(body["time_problem"]["reason"], "same_time")
        self.assertEqual(body["time_problem"]["row"], 2)

    def test_打っている途中は断らない(self) -> None:
        """開始だけ入れた時点で断ると、続きが打てない。"""
        body = self.state({"1": {"KZ": "07", "KH": "00"}})
        self.assertIsNone(body["time_problem"])

    # ---- 重量 --------------------------------------------------------
    def test_枚数と包数から実績合計枚数(self) -> None:
        body = self.state({"1": {"MAI": "3", "TUT": "1"}})
        self.assertEqual(body["rows"]["1"]["CON"], "3")

    def test_単重から作業重量(self) -> None:
        body = self.state({"1": {"MAI": "3", "TUT": "1", "UNI": "250.04314"}})
        self.assertEqual(body["rows"]["1"]["WEI"], "750.1")

    def test_単重をさかのぼる(self) -> None:
        body = self.state({
            "1": {"MAI": "1", "TUT": "1", "UNI": "1001.15495"},
            "2": {"MAI": "1", "TUT": "1"},
        })
        self.assertEqual(body["rows"]["2"]["WEI"], "1001.2")

    def test_直の合計がヘッダーに入る(self) -> None:
        body = self.state({
            "1": {"MAI": "3", "TUT": "1", "UNI": "250.04314"},
            "2": {"MAI": "1", "TUT": "1", "UNI": "1626.87679"},
        })
        self.assertEqual(body["header"]["count"], "4")
        self.assertEqual(body["header"]["weight_kg"], "2377.0")

    def test_量った重量は上書きしない(self) -> None:
        body = self.state({"1": {"MAI": "3", "TUT": "1", "UNI": "250",
                                 "WEI": "755.5"}})
        self.assertEqual(body["rows"]["1"]["WEI"], "755.5")

    def test_打った単重は消えない(self) -> None:
        """**単重 → 重量 と 重量 → 単重 は逆向き。空いているほうだけ埋める。**

        `単重計算` には「単重がすでに入っていたら空にする」枝があり、
        そのまま毎回呼ぶと打った 250.04314 が消えて、次の回に
        重量÷枚数 の 250.03 で入れ直されます。
        """
        rows = {"1": {"LOT": "H5422S0", "MAI": "3", "TUT": "1",
                      "UNI": "250.04314"}}
        for _ in range(3):                 # 欄を離れるたびに送られる
            body = self.state(rows)
            rows = {"1": dict(body["rows"]["1"])}
        self.assertEqual(rows["1"]["UNI"], "250.04314")
        self.assertEqual(rows["1"]["WEI"], "750.1")

    def test_重量から単重も出る(self) -> None:
        """逆向き。量った重量を入れれば単重が出る(`単重計算`)。"""
        body = self.state({"1": {"LOT": "L-1", "MAI": "10", "TUT": "1",
                                 "WEI": "100"}})
        self.assertEqual(body["rows"]["1"]["CON"], "10")
        self.assertEqual(body["rows"]["1"]["UNI"], "10.00")

    def test_断ったら作業時間を全部空にする(self) -> None:
        """VBA も先頭で全部消してから計算し、途中で `Exit Sub` していた。

        前の値を残すと、直したつもりの無い行に古い時間が居座って、
        そのまま保存されます。
        """
        good = self.state({
            "1": {"KZ": "07", "KH": "00", "SZ": "09", "SH": "00"},
            "2": {"KZ": "09", "KH": "00", "SZ": "11", "SH": "00"}})
        self.assertEqual(good["rows"]["1"]["TIM"], "120")
        self.assertEqual(good["rows"]["2"]["TIM"], "120")

        # 2行目を壊す。**1行目の時間も消える**
        bad = self.state({
            "1": {"KZ": "07", "KH": "00", "SZ": "09", "SH": "00", "TIM": "120"},
            "2": {"KZ": "99", "KH": "00", "SZ": "11", "SH": "00", "TIM": "120"}})
        self.assertIsNotNone(bad["time_problem"])
        self.assertEqual(bad["rows"]["1"]["TIM"], "")
        self.assertEqual(bad["rows"]["2"]["TIM"], "")

    def test_単重は打てる(self) -> None:
        """読み取り専用にすると「単重から作業重量」ができなくなる。"""
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        self.assertNotRegex(html, r'id="UNI1"[^>]*readonly')
        # 作業時間はサーバが決めるので読み取り専用のまま
        self.assertRegex(html, r'id="TIM1"[^>]*readonly')

    # ---- 保存まで通る ------------------------------------------------
    def test_計算した値が保存される(self) -> None:
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "H5422S0", "MAI": "3", "TUT": "1",
                           "UNI": "250.04314",
                           "KZ": "22", "KH": "50", "SZ": "00", "SH": "30",
                           "TH": "20"}},
            "header": {"worker": "近藤雅幹"}})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["saved"])

        # 画面を開き直しても、計算済みの値が出る
        html = self.client.get("/", headers=HEADERS).get_data(as_text=True)
        for value in ("750.1", "80"):      # 重量 / 作業時間(100-20)
            with self.subTest(value=value):
                self.assertIn(f'value="{value}"', html)

    def test_直っていない行があると保存を断る(self) -> None:
        """**警告を出すだけでは直らなかった。**

        以前はここで 200 を返していました ── 警告は出るが入力は進められ、
        間違ったまま保存され、気づくのは翌日の集計です。打つのは自由な
        ままにして、関門は「保存(確定)」に置きます。
        """
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "L-1", "KZ": "99"}}, "header": {}})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertIn("直すところがあるので保存できません", body["message"])
        # どの行が悪いかを返す(画面はこれで行に印を付ける)
        self.assertEqual([b["row"] for b in body["bad_rows"]], [1])

    def test_自動保存は止めない(self) -> None:
        """打ちかけを手元に残すためのもの。**ここで止めると何も残らない。**

        共有へ出るのは「共有へ保存」で、そちらは別の関門を通ります。
        """
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "L-1", "KZ": "99"}}, "header": {},
            "silent": True})
        self.assertEqual(res.status_code, 200)

    def test_直せば保存できる(self) -> None:
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "L-1", "KZ": "08", "KH": "00",
                           "SZ": "10", "SH": "00"}}, "header": {}})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["saved"])
        self.assertEqual(res.get_json()["bad_rows"], [])


class ShiftLimitThroughApiTests(WebTestCase):
    """直の規定時間を超えたら断る。"""

    def setUp(self) -> None:
        super().setUp()
        repo = self.repo()
        for key, (start, end) in (("1", ("07:00", "15:00")),
                                  ("2", ("15:00", "22:50")),
                                  ("3", ("22:50", "07:00"))):
            repo.set_shift_time(key, start, end)

    def test_超えたら断る(self) -> None:
        res = self.post("/api/entry/state", {
            "rows": {"1": {"KZ": "00", "KH": "00", "SZ": "23", "SH": "00"}},
            "header": {}})
        problem = res.get_json()["time_problem"]
        self.assertIsNotNone(problem)
        self.assertEqual(problem["reason"], "over_shift")
        self.assertIn("規定時間", problem["message"])

    def test_管理者モードなら全直の最大まで通す(self) -> None:
        """他の直のデータを開いて精査するので、いまの直で縛らない。"""
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/entry/state", {
            # 485分 = 8時間5分。1直(480)は超えるが3直(490)には収まる
            "rows": {"1": {"KZ": "00", "KH": "00", "SZ": "08", "SH": "05"}},
            "header": {}})
        self.assertIsNone(res.get_json()["time_problem"])
