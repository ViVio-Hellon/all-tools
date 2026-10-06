"""保存のあとの書き出し ── **押し忘れても、共有のCSVが古くならない**

【なぜ自動で書くのか】
「集計CSVを出力」のボタンは残してあります。ただ、押し忘れると共有の
フォルダは前の直のままです。**古いCSVは、無いCSVより危ない** ──
開いた人はそれが最新だと思って読みます。集計(`packing_report`)を保存の
たびに作り直しているのと同じ理由で、書き出しも同じ場所に置きました。

【なぜ自動保存では書かないのか】
自動保存は欄から焦点が外れるたびに走ります。そのたびに共有のフォルダへ
4本書くと、**打っている最中ずっと書き続ける**ことになります。

【なぜ落ちても保存が成功なのか】
書き出し先が共有で、そこへ届かないことは普通にあります。CSVが出ない
ことより、打った12行が消えるほうが困ります。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase


def row(**extra) -> dict:
    base = {"LOT": "N7131T0", "KZ": "08", "KH": "00", "SZ": "10", "SH": "00",
            "CON": "10", "WEI": "1000", "MAI": "10"}
    base.update(extra)
    return base


def payload(**extra) -> dict:
    body = {"rows": {"1": row()}, "header": {"worker": "山田"}, "checks": {}}
    body.update(extra)
    return body


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AutoExportTests(WebTestCase):
    """保存 → 集計計算 → **書き出し**。"""

    def out_dir(self) -> Path:
        from nippou.config import SETTINGS

        return SETTINGS.report_output_dir

    def written(self) -> list[Path]:
        """出力先にできたCSVを全部(年/月/日 の下まで潜る)。"""
        base = self.out_dir()
        return sorted(base.rglob("*.csv")) if base.is_dir() else []

    # -- 出るところ ----------------------------------------------------
    def test_保存を押すと日のフォルダに3本_年月のフォルダに説明2つ(self) -> None:
        """日フォルダ内に毎回入れる CSVの読み方、計算内容は年月フォルダに
        1回だけ入れてください 無ければ入れる"""
        body = self.post("/api/entry/save", payload()).get_json()
        self.assertTrue(body["saved"])
        self.assertTrue(body["export"]["ok"], body["export"])
        day = Path(body["export"]["dir"])
        self.assertEqual(sorted(p.name.split("_")[0] for p in day.iterdir()),
                         sorted(["集計", "集計明細", "停止内訳"]))
        self.assertEqual(sorted(p.name for p in day.parent.iterdir() if p.is_file()),
                         sorted(["CSVの読み方.txt", "計算内容.csv"]))
        self.assertIn("年月のフォルダに", body["export"]["message"])

    def test_ライン年月日のフォルダに入る(self) -> None:
        """**指定のパスの下に ライン/集計/年月/日。** 1フォルダに溜めない。

        ラインを噛ませているのは、出力先に**共有のフォルダを指せる**
        からです。全ラインが同じ所へ書くと、自分のラインのぶんを探すのに
        ファイル名の後ろを1件ずつ読むことになります。
        """
        body = self.post("/api/entry/save", payload()).get_json()
        folder = Path(body["export"]["dir"])
        line, kind, month, day = folder.parts[-4:]
        self.assertEqual(folder.parent.parent.parent.parent, self.out_dir())
        self.assertEqual(line, "L-1")
        self.assertEqual(kind, "集計")
        self.assertRegex(month, r"^\d{4}\.\d{2}$")   # 2桁で揃える
        self.assertRegex(day, r"^\d{2}$")

    def test_3本とも同じフォルダ(self) -> None:
        """開くのはファイルではなくフォルダなので、散らばらせない。"""
        self.post("/api/entry/save", payload())
        days = {p.parent for p in self.written() if p.name != "計算内容.csv"}
        self.assertEqual(len(days), 1)

    def test_出し先を画面に返す(self) -> None:
        """出たことも、どこに出たかも**画面に残す**(知らせは消える)。"""
        body = self.post("/api/entry/save", payload()).get_json()
        self.assertIn(body["export"]["dir"], body["export"]["message"])

    def test_押す前から出し先が見えている(self) -> None:
        """「どこに出てます?」に、押したあとでなく**押す前**に答える。"""
        html = self.get("/").get_data(as_text=True)
        self.assertIn("出力先:", html)
        self.assertIn("ライン名 / 集計 / 年月 / 日", html)

    # -- 出ないところ --------------------------------------------------
    def test_自動保存では書かない(self) -> None:
        """**打っている最中ずっと書き続けない。**"""
        body = self.post("/api/entry/save", payload(silent=True)).get_json()
        self.assertTrue(body["saved"])
        self.assertIsNone(body["export"])
        self.assertEqual(self.written(), [])

    def test_断られた保存では書かない(self) -> None:
        """直っていない行があると保存は 422。**書き出しもしない。**"""
        res = self.post("/api/entry/save",
                        {"rows": {"1": row(TH="900")},
                         "header": {"worker": "山田"}, "checks": {}})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.written(), [])

    # -- 落ちたところ --------------------------------------------------
    def test_書き出しに失敗しても保存は成功(self) -> None:
        """**打った12行が消えるほうが困る。** 握りつぶして、画面に出す。"""
        from nippou.reporting import csv_export

        with patch.object(csv_export, "write_daily_set",
                          side_effect=OSError("共有に届きません")):
            body = self.post("/api/entry/save", payload()).get_json()

        self.assertTrue(body["saved"])            # 日報は書けている
        self.assertFalse(body["export"]["ok"])
        self.assertIn("共有に届きません", body["export"]["message"])
        self.assertEqual(self.repo().list_keys() != [], True)

    def test_失敗を黙って飲み込まない(self) -> None:
        """出ていないことは**画面に出す** ── 出たと思わせない。"""
        from nippou.reporting import csv_export

        with patch.object(csv_export, "write_daily_set",
                          side_effect=OSError("x")):
            body = self.post("/api/entry/save", payload()).get_json()
        self.assertIn("集計CSVは出せませんでした", body["export"]["message"])

    # -- 中身 ----------------------------------------------------------
    def test_押して出すのと同じものが出る(self) -> None:
        """ボタンと自動で中身が違うと、**どちらが正か分からなくなる。**"""
        self.post("/api/entry/save", payload())
        auto = {p.name: p.read_bytes() for p in self.written()}

        body = self.post("/api/graph/csv", {}).get_json()
        self.assertEqual(body["count"], 1)
        pressed = {p.name: p.read_bytes() for p in self.written()}
        self.assertEqual(set(auto), set(pressed))
        for name in auto:
            with self.subTest(file=name):
                self.assertEqual(auto[name], pressed[name])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LegendTests(WebTestCase):
    """`CSVの読み方.txt` が消えていないか ── **消されたら出し直す。**

    共有のフォルダに置くものなので消されます。「よく分からないファイル」
    として片付けられることも、フォルダごと整理されることもあります。
    """

    def out_dir(self) -> Path:
        from nippou.config import SETTINGS

        return SETTINGS.report_output_dir

    def legend(self) -> Path:
        from nippou.reporting import csv_legend

        return next(self.out_dir().rglob(csv_legend.FILENAME))

    def test_年月のフォルダに1つ出る(self) -> None:
        """日のフォルダの1つ上(年月)。**日のフォルダには置かない。**"""
        self.post("/api/entry/save", payload())
        self.assertTrue(self.legend().is_file())
        day = Path(self.post("/api/graph/csv", {}).get_json()["dir"])
        self.assertEqual(self.legend().parent, day.parent)
        self.assertEqual(len(list(self.out_dir().rglob("CSVの読み方.txt"))), 1)

    def test_消したら保存で出し直す(self) -> None:
        self.post("/api/entry/save", payload())
        self.legend().unlink()

        body = self.post("/api/entry/save", payload()).get_json()
        self.assertTrue(body["export"]["legend_reissued"])
        self.assertIn("年月のフォルダに CSVの読み方.txt を置きました",
                      body["export"]["message"])
        self.assertTrue(self.legend().is_file())

    def test_消していなければ黙っている(self) -> None:
        """毎回「出し直しました」と言われると、言葉が効かなくなります。"""
        self.post("/api/entry/save", payload())
        body = self.post("/api/entry/save", payload()).get_json()
        self.assertFalse(body["export"]["legend_reissued"])
        self.assertNotIn("出し直し", body["export"]["message"])

    def test_消したらボタンでも出し直す(self) -> None:
        self.post("/api/entry/save", payload())
        self.legend().unlink()
        body = self.post("/api/graph/csv", {}).get_json()
        self.assertTrue(body["legend_reissued"])
        self.assertTrue(self.legend().is_file())

    def test_過ぎた日のぶんもボタンで配り直す(self) -> None:
        """**書くたびに見るのはその日のフォルダだけ。**

        先月の3日から消されても、そこへはもう書かないので気づけません。
        押したときが、まとめて直せる唯一の機会です。
        """
        from nippou.reporting import csv_legend

        self.post("/api/entry/save", payload())
        old = self.out_dir() / "L-1" / "集計" / "2025.12" / "31"
        old.mkdir(parents=True)
        (old / "集計_2025-12-31_L-1.csv").write_text("日付\n", encoding="utf-8-sig")

        body = self.post("/api/graph/csv", {}).get_json()
        self.assertTrue((old.parent / csv_legend.FILENAME).is_file())
        self.assertTrue((old.parent / csv_legend.GUIDE_FILENAME).is_file())
        self.assertFalse((old / csv_legend.FILENAME).exists())
        self.assertIn(str(old.parent / csv_legend.FILENAME),
                      body["legend_sweep"]["reissued"])
        self.assertIn("欠けていた", body["message"])

    def test_揃っていれば画面に何も足さない(self) -> None:
        self.post("/api/graph/csv", {})
        body = self.post("/api/graph/csv", {}).get_json()
        self.assertEqual(body["legend_sweep"]["reissued"], [])
        self.assertNotIn("欠けていた", body["message"])

    def test_配り直せなくてもCSVは出る(self) -> None:
        """説明が置けないことで、出力そのものを失敗にはしません。"""
        from nippou.reporting import csv_legend

        with patch.object(csv_legend, "sweep", side_effect=OSError("共有が遠い")):
            res = self.post("/api/graph/csv", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["legend_sweep"]["checked"], 0)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class StopKindNameTests(unittest.TestCase):
    """分類の名前 ── **見せる名前と、溜めてある文字は別。**"""

    def test_溜めてある文字は変えない(self) -> None:
        """`packing_stop_detail.stop_kind` に入っている文字です。

        **ここを書き換えると、去年保存した行が全部「その他」に落ちます**
        (`packing_agg.by_key` がこの文字で足しているため)。見せる名前を
        変えたいときは `STOP_KIND_LABELS` のほうだけ変えてください。
        """
        from nippou.logic.aggregation import STOP_KINDS

        self.assertEqual(STOP_KINDS, {
            "数値": "管理ロス停止",
            "カタカナ": "突発停止",
            "アルファベット": "ハンドリング停止",
        })

    def test_見せる名前は現場の呼び名(self) -> None:
        from nippou.logic.aggregation import stop_kind_label

        self.assertEqual(stop_kind_label("管理ロス停止"), "管理ロス設備停止")
        self.assertEqual(stop_kind_label("突発停止"), "段取り・突発停止")
        self.assertEqual(stop_kind_label("ハンドリング停止"), "ハンドリング停止")

    def test_知らない分類はそのまま返す(self) -> None:
        from nippou.logic.aggregation import stop_kind_label

        self.assertEqual(stop_kind_label("なにか"), "なにか")
        self.assertEqual(stop_kind_label(""), "その他")

    def test_並びはマスタの順(self) -> None:
        """重い順でも五十音でもなく 作業停止時間内訳_1 → _2 → _3。"""
        from nippou.logic.aggregation import stop_kind_sort_key

        kinds = ["ハンドリング停止", "その他", "管理ロス停止", "突発停止"]
        self.assertEqual(sorted(kinds, key=stop_kind_sort_key),
                         ["管理ロス停止", "突発停止", "ハンドリング停止",
                          "その他"])

    def test_画面とCSVで同じ名前(self) -> None:
        """凡例とCSVの見出しが食い違うと、突き合わせるたびに読み替える。"""
        from nippou.reporting import csv_export

        self.assertIn("管理ロス設備停止(分)", csv_export.FIELDNAMES)
        self.assertIn("段取り・突発停止(分)", csv_export.FIELDNAMES)


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
