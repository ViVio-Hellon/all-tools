"""数字の種類と、画面の役割 ── **色の意味を決めるのはサーバ**

【なぜこのテストがあるか】
「配色を認知心理も意識し変えてください　全部同じで気持ち悪いです」と
言われたのがもとです。同じ色で7つ並ぶと目が滑って**どれも読まない**
画面になり、かといって虹色にすると色が意味を失います。

ここで守るのは、**色そのもの**ではなく「何がどの種類か」です。色の値は
`tokens.css` の1か所にあり、画面は `data-tone` / `data-role` を写すだけ。
だからここは「重量は出来高か」「停止は停止か」を見ます ── 見た目を
直したい日に、この判断が巻き添えで壊れないように。

【色だけに頼らない】
種類は色のほかに**縁の線**で、役割は色のほかに**題の隣の一言**で出して
います。色が見分けられなくても同じことが伝わるか、も併せて見ます。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.aggregation import ShiftAggregate
from nippou.presenters import dashboard as dash
from tests._web import HEADERS, WebTestCase

DAY = "2026年8月3日"


def _rows() -> list[ShiftAggregate]:
    def one(shift, **kw):
        return ShiftAggregate(report_date=DAY, line="L-1", shift=shift, **kw)

    return [
        one("1直", sheet_count=120, weight_kg=14400, work_minutes=400,
            management_loss_minutes=60, unplanned_stop_minutes=30,
            handling_stop_minutes=20),
        one("2直", sheet_count=100, weight_kg=12000, work_minutes=380,
            management_loss_minutes=40, unplanned_stop_minutes=10,
            handling_stop_minutes=5),
        one("3直", sheet_count=90, weight_kg=10500, work_minutes=360,
            management_loss_minutes=20),
    ]


class NumberToneTests(unittest.TestCase):
    """7つの数字が、量の種類で分かれているか。"""

    def tones(self) -> dict[str, str]:
        tiles = dash.today_tiles(_rows(), DAY, cumulative=1200)
        return {t.key: t.tone for t in tiles}

    def test_枚数のなかまは枚数(self) -> None:
        tones = self.tones()
        for key in ("today_weight", "today_count", "today_productivity"):
            self.assertEqual(tones[key], dash.TONE_QTY, key)

    def test_累積のタイルは累積の線と同じ色(self) -> None:
        """**数字と絵が同じ色で繋がる**のが要点です。

        ここは `TONE_GOAL` でした。累積と45度線は「対で読むもの」なので
        同じなかまに置く、という理屈です。ですが v3.44.0 で**線のほう
        だけ**を `TONE_CUMULATIVE` に分けたので、タイルと線が別の色に
        なっていました ── 同じ「累積枚数」なのに。

        45度線が赤になって(v3.49.0)、その食い違いが目に出ました。
        **赤は45度線1本の色**なので、タイルは線のほうへ揃えます。
        """
        self.assertEqual(self.tones()["cumulative_count"],
                         dash.TONE_CUMULATIVE)
        # 絵の側(複合グラフの累積の線)と同じ印であること
        trend = dash.cumulative_tile([], "")
        line = next(s for s in trend.series if s.key == "cumulative")
        self.assertEqual(line.mark, dash.TONE_CUMULATIVE)
        # 塗る口がCSSにあること(印だけあっても色は出ません)
        css = (Path(__file__).resolve().parent.parent / "app" / "static"
               / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn('.tile[data-tone="cumulative"]{ --tone:var(--cumulative); }',
                      css)

    def test_時間のなかまは時間(self) -> None:
        tones = self.tones()
        self.assertEqual(tones["today_rate"], dash.TONE_DUR)
        self.assertEqual(tones["today_operating"], dash.TONE_DUR)

    def test_使う色は4つまで(self) -> None:
        """**色数を増やさない。** 量の種類はこの4つしかない。"""
        used = {v for v in self.tones().values()
                if v in dash.QUANTITY_TONES}
        self.assertLessEqual(len(used), 4)
        self.assertEqual(set(dash.QUANTITY_TONES),
                         {dash.TONE_QTY, dash.TONE_DUR, dash.TONE_STOP,
                          dash.TONE_GOAL})

    def test_停止は停止(self) -> None:
        self.assertEqual(self.tones()["today_stop"], dash.TONE_STOP)

    def test_全部同じにはならない(self) -> None:
        """**ここが本題。** 7つ並んで1色だと、どれも読まない画面になる。"""
        self.assertGreaterEqual(len(set(self.tones().values())), 3)

    def test_良し悪しは言わない(self) -> None:
        """稼働率が何%ならよいかはラインごとに違う ── ツールは知らない。

        だから数字の大小で色は変えません。**種類までで止める。**
        """
        low = [ShiftAggregate(report_date=DAY, line="L-1", shift="1直",
                              sheet_count=1, weight_kg=100, work_minutes=10,
                              management_loss_minutes=900)]
        high = _rows()
        self.assertEqual(
            dash.today_tiles(low, DAY)[2].tone,
            dash.today_tiles(high, DAY)[2].tone)


class PendingToneTests(unittest.TestCase):
    """共有へ未保存 ── **ここだけは良し悪しを言う。**"""

    def test_0件は済んでいる(self) -> None:
        self.assertEqual(dash.pending_tile(0).tone, dash.TONE_OK)

    def test_1件以上は残っている(self) -> None:
        self.assertEqual(dash.pending_tile(1).tone, dash.TONE_TODO)
        self.assertEqual(dash.pending_tile(33).tone, dash.TONE_TODO)

    def test_0でも出す(self) -> None:
        """消すと「押し忘れか、そもそも無いのか」が分からない。"""
        self.assertEqual(dash.pending_tile(0).value, "0")


class ShiftColorTests(unittest.TestCase):
    """直別の棒は**直の色**で塗る。同じ画面で1直の色は1つ。"""

    def test_棒を直の色で塗る(self) -> None:
        """**ここが直したところ。**                        [v3.51.0]

        > 同じカテゴリは似た色のルールは撤廃！
        > カテゴリ一緒でも分けてなきゃ意味ないでしょ

        一度は「どの棒がどの直かは横軸に書いてあるのだから、色でも言う
        のは意味のない多色使い」として1色にしていました。上の複合
        グラフで直を色で積む以上、ここだけ1色だと**同じ画面の中で
        1直の色が2通り**になります。
        """
        weight, stop = dash.shift_tiles(_rows())
        want = ["shift-1", "shift-2", "shift-3"]
        self.assertEqual(weight.point_classes, want)
        self.assertEqual(stop.point_classes, want)

    def test_直は横軸にも出る(self) -> None:
        """色と字の**両方**で言う。どちらか片方に頼らない。"""
        weight, _ = dash.shift_tiles(_rows())
        self.assertEqual(weight.labels, ["1直", "2直", "3直"])

    def test_2枚は棒の色で見分ける(self) -> None:
        """重量の絵は緑、停止の絵は暖色。**題を読む前に役割が分かる。**"""
        weight, stop = dash.shift_tiles(_rows())
        self.assertEqual(weight.tone, dash.TONE_QTY)
        self.assertEqual(stop.tone, dash.TONE_STOP)

    def test_直の色は1か所で決める(self) -> None:
        """表・帯・グラフで**同じ色**。別に持つと必ず食い違います。"""
        self.assertEqual(dash.SHIFT_CLASSES["1直"], "shift-1")
        self.assertEqual(dash.SHIFT_CLASSES["日勤"], "shift-day")
        # 積み上げた段も同じ表から引く(`SHIFT_QTY_CLASSES is SHIFT_CLASSES`)
        self.assertIs(dash.SHIFT_QTY_CLASSES, dash.SHIFT_CLASSES)


class StopKindColorTests(unittest.TestCase):
    """停止の3分類は**入れ替え禁止**の色。並び順で決めない。"""

    def test_分類ごとに決まった色(self) -> None:
        tile = dash.stop_kind_tile(_rows())
        marks = {s["label"]: s["mark"] for s in tile.slices}
        # 凡例に出るのは**現場の呼び名**(`STOP_KIND_LABELS`)
        self.assertEqual(marks["管理ロス設備停止"], "stop-loss")
        self.assertEqual(marks["段取り・突発停止"], "stop-sudden")
        self.assertEqual(marks["ハンドリング停止"], "stop-handling")

    def test_大きさが入れ替わっても色は動かない(self) -> None:
        """**ここが直したところ。**

        順番の色に任せていたので、長い順に並べ替えた拍子に突発停止が
        管理ロスと同じ色で出ていました。日によって分類の色が変わると、
        「赤は突発」という現場の記憶がそのまま読み違いになります。
        """
        flipped = [ShiftAggregate(report_date=DAY, line="L-1", shift="1直",
                                  sheet_count=1, weight_kg=1000,
                                  work_minutes=100,
                                  management_loss_minutes=5,
                                  unplanned_stop_minutes=500,
                                  handling_stop_minutes=50)]
        marks = {s["label"]: s["mark"]
                 for s in dash.stop_kind_tile(flipped).slices}
        self.assertEqual(marks["段取り・突発停止"], "stop-sudden")
        self.assertEqual(marks["管理ロス設備停止"], "stop-loss")

    def test_項目は1つずつ別の色で塗る(self) -> None:
        """**分類の色で塗るのをやめたところ。**

        一度は項目が属する3分類の色で塗っていました。ひと目で「計画
        された停止か、異常か」が読める代わりに、

            0 休憩食事 / 2 人員不足 / 6 教育 / 1 TPM活動・清掃

        が**4つとも同じ水色**になります ── 凡例を1行ずつ目で追わないと、
        輪のどこがどれか分かりません。見分けるための絵で見分けられない。

        いまは指定の10色を上から順に配ります。分類のほうは、すぐ上の
        「停止の内訳(分類)」が3色で受け持つので、**どちらの読み方も
        別々の絵で残ります。**
        """
        from nippou.logic.packing_agg import StopRow

        items = [
            (StopRow(stop_no=1, stop_code="0", stop_reason="休憩食事",
                     stop_kind="管理ロス停止", stop_minutes=60.0), 2),
            (StopRow(stop_no=2, stop_code="2", stop_reason="人員不足",
                     stop_kind="管理ロス停止", stop_minutes=30.0), 1),
            (StopRow(stop_no=3, stop_code="6", stop_reason="教育",
                     stop_kind="管理ロス停止", stop_minutes=10.0), 1),
        ]
        slices = dash.stop_item_tile(items).slices
        marks = [s["mark"] for s in slices]
        # **ここが本体。** 同じ分類の3つでも、色は3つとも別
        self.assertEqual(len(set(marks)), 3, marks)
        # 上から順に配る(輪は長い順に並ぶ)
        self.assertEqual(marks, ["cat-1", "cat-2", "cat-3"])

    def test_その他は専用の色(self) -> None:
        """まとめたぶんは主役ではないので、色を主張させない。

        以前は色が付かず、暗い地では**黒い扇**になって沈んでいました。
        """
        from nippou.logic.packing_agg import StopRow

        items = [(StopRow(stop_no=i, stop_code=str(i), stop_reason=f"停止{i}",
                          stop_kind="管理ロス停止",
                          stop_minutes=float(100 - i)), 1)
                 for i in range(1, 10)]
        slices = dash.stop_item_tile(items).slices
        rest = slices[-1]
        self.assertEqual(rest["label"], dash.REST_LABEL)
        self.assertEqual(rest["mark"], dash.CAT_REST)

    def test_同じ絵の中で色がぶつからない(self) -> None:
        """**扇の数だけ別の色がある。** 1枚の中で色が重なると、
        どれがどれか分かりません。"""
        from nippou.logic.packing_agg import StopRow

        items = [(StopRow(stop_no=i, stop_code=str(i), stop_reason=f"停止{i}",
                          stop_kind="管理ロス停止",
                          stop_minutes=float(100 - i)), 1)
                 for i in range(1, 8)]
        marks = [s["mark"] for s in dash.stop_item_tile(items).slices]
        self.assertEqual(len(set(marks)), len(marks), marks)

    def test_項目が無ければ空(self) -> None:
        self.assertEqual(dash.stop_item_tile([]).slices, [])


class ComboTests(unittest.TestCase):
    """**棒と線は色を分ける。**

    一度どちらも枚数の緑にしましたが、線が棒を横切るところで棒に溶け
    ました。見ている先も違います ── 棒は「その日いくつ出たか」、線は
    「目標に対してどこまで来たか」。
    """

    def points(self):
        from nippou.logic.packing_agg import DayPoint

        return [DayPoint(work_date=DAY, shifts=1, quantity=100.0,
                         cumulative_quantity=100.0),
                DayPoint(work_date="2026年9月13日", shifts=1, quantity=120.0,
                         cumulative_quantity=220.0)]

    def test_棒と線でなかまが違う(self) -> None:
        tile = dash.cumulative_tile(self.points(), "9月")
        marks = {s.key: s.mark for s in tile.series}
        self.assertEqual(marks["count"], dash.TONE_QTY)
        self.assertEqual(marks["cumulative"], dash.TONE_CUMULATIVE)
        self.assertNotEqual(marks["count"], marks["cumulative"])

    def test_形も違う(self) -> None:
        """色に加えて形も違えておく。**色だけに頼らない。**"""
        kinds = {s.key: s.kind for s in dash.cumulative_tile(self.points(), "9月").series}
        self.assertEqual(kinds["count"], "bar")
        self.assertEqual(kinds["cumulative"], "line")

    def test_目標線と累積線は色でも分ける(self) -> None:
        """**対で読むもの**ですが、色まで同じにはしません。

        一度は同じ紫にして、実線/破線だけで分けていました。線が2本
        重なる絵で線種だけを頼りにするのは細すぎます ── 点が詰まると
        実線も破線も同じ帯に見え、どちらが目標か分からなくなります。
        """
        tile = dash.cumulative_tile(self.points(), "9月")
        dash.add_targets([tile], 150.0)
        target = next(s for s in tile.series if s.key == "target")
        cumulative = next(s for s in tile.series if s.key == "cumulative")
        self.assertEqual(target.mark, dash.TONE_GOAL)
        self.assertEqual(target.kind, "line")
        # **ここが本体。** 3本とも別の色
        marks = [s.mark for s in tile.series]
        self.assertEqual(len(set(marks)), 3, marks)


class ScreenTests(WebTestCase):
    """画面がそれを写しているか。**色だけに頼っていないか。**"""

    def test_グラフ画面に種類が出る(self) -> None:
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn('data-tone="qty"', body)
        self.assertIn('data-tone="dur"', body)
        self.assertIn('data-tone="stop"', body)

    def test_GWの4列に役割が付く(self) -> None:
        body = self.get("/gw").get_data(as_text=True)
        for role in ("read", "input", "choose", "result"):
            self.assertIn(f'data-role="{role}"', body, role)

    def test_役割は字でも出る(self) -> None:
        """**色だけに頼らない。** 色が見分けられなくても同じことが伝わる。"""
        body = self.get("/gw").get_data(as_text=True)
        for word in ("引いてきた値", "打つ", "選ぶ", "出てきた値"):
            self.assertIn(word, body, word)


class TokenTests(unittest.TestCase):
    """色の値は `tokens.css` の1か所。**ここ以外に直値を書かない。**"""

    def css(self, name: str) -> str:
        return (Path(__file__).resolve().parent.parent
                / "app" / "static" / "css" / name).read_text(encoding="utf-8")

    def test_新しい色がトークンにある(self) -> None:
        text = self.css("tokens.css")
        for name in ("--qty", "--dur", "--stop",
                     "--role-read", "--role-input", "--role-choose",
                     "--role-result"):
            self.assertIn(f"{name}:", text, name)

    def test_暗い画面ぶんもある(self) -> None:
        """3つの状態(既定 / OSがダーク / 明示指定)のどれでも出る。

        **ネオンの層は数に入れません**(v3.51.0)。あれは集計・グラフの
        画面にだけ当たる上書きで、3つの状態とは別の軸です ── 読むのは
        `:root[data-skin="neon"]` より前の部分だけにします。
        """
        text = self.css("tokens.css")
        text = text[:text.index(':root[data-skin="neon"]')]
        for name in ("--qty", "--dur", "--stop", "--goal",
                     "--role-choose",
                     "--stop-loss", "--stop-sudden", "--stop-handling",
                     "--shift-1", "--shift-day"):
            self.assertEqual(text.count(f"{name}:"), 3, name)

    def test_順番の色は捨てた(self) -> None:
        """**8色の回し使いをやめた。**

        回していたころは、長い順に並べ替えた拍子に同じ項目が日によって
        違う色になり、色が何も意味しませんでした。名前ごと消してある
        ことを確かめます ── 残しておくと、次に誰かが使います。
        """
        self.assertNotIn("--series-", self.css("tokens.css"))
        self.assertNotIn("series-0", self.css("components.css"))

    def test_検証を通した値がそのまま入っている(self) -> None:
        """**この値は目で決めていません。**

        OKLCH の明度帯・彩度下限・P型/D型色覚での距離(ΔE)・地との
        コントラストを検証器に通した値です。「もう少し明るく」で1段
        ずらすと、静かに見分けられなくなります ── ずらすときは検証器へ
        通し直して、ここも一緒に書き換えてください。

        ここで固定しているのは**明るい画面のぶん**だけです(暗い画面は
        別の帯で取り直してあり、値も別)。
        """
        text = self.css("tokens.css")
        for name, value in (
                ("--qty", "#00916f"),      # 枚数のなかま(緑)
                # 45度線(目標)。**「45度線赤にして」** ── 現場の指定で
                # 紫から赤へ。この絵に暖色は出てこない(棒は緑、累積は
                # teal)ので置けます
                ("--goal", "#CC3311"),
                ("--dur", "#3355cc"),      # 時間のなかま(青)
                ("--stop", "#cf6a22"),     # 停止(暖色)
                # 停止の3分類。重さの順に 青→橙→赤。**色相で分ける**
                ("--stop-loss", "#1f7fa8"),
                ("--stop-handling", "#d47c20"),
                ("--stop-sudden", "#b5202c"),
                # 直。**Excelのグラフと同じ 青・赤・緑・紫**(v3.51.0)
                # ── 時間の青の濃淡4段では、並べて見分けられませんでした
                ("--shift-1", "#4F81BD"),
                ("--shift-2", "#C0504D"),
                ("--shift-3", "#9BBB59"),
                ("--shift-day", "#8064A2")):
            self.assertIn(f"{name}:{value}", text.replace(" ", ""), name)

    def test_直はExcelと同じ青赤緑紫(self) -> None:
        """**「同じ分類の中は似た色」をやめたところ。**   [v3.51.0]

        > 同じカテゴリは似た色のルールは撤廃！
        > エクセルグラフの配色のようにやってよ

        直は時間の色(青)の濃淡4段でした。「直は時間の区切りだから」と
        いう理屈は通っていますが、**並べて見分けるには弱すぎました** ──
        グラフで隣り合って比べるものです。Office の既定色(Excel の
        グラフがそのまま使う並び)を採ります。
        """
        text = self.css("tokens.css").replace(" ", "")
        for name, want in (("--shift-1", "#4F81BD"), ("--shift-2", "#C0504D"),
                           ("--shift-3", "#9BBB59"), ("--shift-day", "#8064A2")):
            with self.subTest(name=name):
                self.assertIn(f"{name}:{want};", text)
        # 時間の青の濃淡ではなくなったこと(戻すと同じ失敗をします)
        self.assertNotIn("--shift-2:#3355cc", text)

    def test_停止の3分類は色相が違う(self) -> None:
        """**「色味が同じすぎる」と言われたところ。**

        一度これを暖色1色の濃淡3段にしました。濃淡は「量の順番」を出す
        持ち方で、**どれがどれか**を見分けるには色相が要ります ──
        3分類は順番ではなく種類です。

        重さの順に 青(計画された停止) → 橙(段取り) → 赤(異常) と並べます。
        色が近いままだと、日をまたいで「どれが突発だったか」が読めません。
        """
        text = self.css("tokens.css").replace(" ", "")
        for name, value in (("--stop-loss", "#1f7fa8"),
                            ("--stop-handling", "#d47c20"),
                            ("--stop-sudden", "#b5202c")):
            self.assertIn(f"{name}:{value}", text, name)
        # 代表色(タイルの縁・直別の棒)は橙のまま。**停止=悪いことにしない**
        self.assertIn("--stop:#cf6a22", text)

    def test_目標線は種類色に負けない(self) -> None:
        """**日別の枚数推移の目標線が緑になっていました。**

        `.tile[data-tone] .chart .line` のほうが `.chart .line--target`
        より詳しいので、破線の灰が種類色に上書きされていたためです。
        同じ詳しさで書いて勝たせます ── 目標が実績と同じ色に見えると、
        45度線を引いた意味がありません。
        """
        text = self.css("components.css")
        self.assertIn(".tile[data-tone] .chart .line--target", text)

    def test_線が棒に溶けない仕掛けがある(self) -> None:
        """**「45度線と棒グラフが同じ色で見にくい」と言われたところ。**

        棒と線をどちらも枚数の緑にしたら、線が棒を横切るところで棒に
        溶けました。手は2つで、どちらか片方では足りません。

        1. **色を分ける** ── 棒は枚数のなかま(緑)、線は目標のなかま(紫)。
           見ている先が違う(その日の量 / 目標に対する進み具合)ので、
           色を分けても意味は壊れません
        2. **線に地の色の縁を付ける** ── SVGの線に外側の縁は無いので、
           同じ道を2回描いて作る(`chart.js` の `line--halo`)
        """
        css = self.css("components.css")
        self.assertIn("--goal", css)
        self.assertIn(".chart .line--halo", css)
        # 種類色に負けないよう、同じ詳しさでも書いてある
        self.assertIn(".tile[data-tone] .chart .line--halo", css)

        js = (Path(__file__).resolve().parent.parent
              / "app" / "static" / "js" / "chart.js").read_text(encoding="utf-8")
        self.assertIn("line--halo", js)

    def test_棒と線でなかまが違う(self) -> None:
        """棒は枚数の緑、線は目標の紫。**見本も絵と同じ色にする。**"""
        css = self.css("components.css").replace(" ", "")
        self.assertIn(".chart.bar.mark-qty{fill:var(--qty);}", css)
        self.assertIn(".chart.line.mark-goal{stroke:var(--goal);}", css)
        self.assertIn(".dlg__swatch.mark-qty{background:var(--qty);}", css)
        self.assertIn(".dlg__swatch--line.mark-goal{color:var(--goal);}", css)

    def test_目標線は破線(self) -> None:
        """線種でも見分ける。**色だけに頼らない。**

        太さは 2.5 → 3 にしました(v3.51.0 の指定「折れ線は太さ 3〜4」)。
        """
        css = self.css("components.css").replace(" ", "")
        self.assertIn("stroke:var(--goal);stroke-width:3;", css)
        self.assertIn("stroke-dasharray:74", css.replace(" ", ""))

    def test_色だけに頼らない仕掛けがある(self) -> None:
        """**同じなかまは同じ色になる。** だから形の差が要ります。

        ・凡例の見本を線の形にする      `dlg__swatch--line`
        ・目標は破線                    `line--target` / `dlg__swatch--target`
        ・輪と輪のあいだに地の色の隙間  `donut__slice` の stroke
        """
        text = self.css("components.css")
        self.assertIn(".dlg__swatch--line", text)
        self.assertIn(".dlg__swatch--target", text)
        self.assertIn("stroke-dasharray", text)
        # 隙間は2px。1px だと濃淡が並んだときに境目が消える
        self.assertRegex(text, r"\.donut__slice\{[^}]*stroke-width:2")

    def test_部品側は変数だけを使う(self) -> None:
        """新しく足したところに色の直値(#rrggbb)を書いていないこと。"""
        text = self.css("components.css")
        for line in text.splitlines():
            if "--tone" in line or "--role" in line or "mark-" in line:
                self.assertNotRegex(line, r"#[0-9a-fA-F]{3,8}\b", line)


if __name__ == "__main__":
    unittest.main()


class PaletteTests(unittest.TestCase):
    """並べて見分けるための10色。**指定された並びのまま持つ。**

    現場から色コードで指定を受けた組です(Paul Tol の muted と同じ並び)。
    P型/D型色覚でも互いに見分けられるように作られていて、**色相の配り
    かたがその性質を作っています** ── こちらで1色ずらすと、静かに
    見分けられなくなります。
    """

    #: 指定された並び。**この表がそのまま `tokens.css` に入っている**
    GIVEN = ["#332288", "#88CCEE", "#44AA99", "#117733", "#999933",
             "#DDCC77", "#CC6677", "#882255", "#AA4499", "#DDDDDD"]

    def css(self) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "static"
                / "css" / "tokens.css").read_text(encoding="utf-8")

    def test_明るい地では指定のまま(self) -> None:
        css = self.css()
        for i, want in enumerate(self.GIVEN, start=1):
            with self.subTest(n=i):
                self.assertIn(f"--cat-{i}:{want};", css)

    def test_45度線は10色の外から採る(self) -> None:
        """**この1本だけの例外。** 現場が名指しで許したところです。

        > 45度線 — 赤だけ特別に許してください　onoffあるんだし

        10色は**並べて見分ける**ためのもので、45度線は並べるものでは
        なく、1本だけ意味の違う線です。同じ組から採ると「11色目」に
        見えます。

        逃げ道があるのも根拠のうちです ── 45度線はグラフ画面の
        「目標線(45度線)」で切れる(VBA `UFdaily.Targetline` と同じ)ので、
        混んで読みにくい日は消せます。**消せない色**(棒・停止の内訳・
        凡例)を10色の外から採るのとは話が別です。

        「配色は `--cat-*` に揃える」という整理は筋が通って見えますが、
        それをやると現場の指定を黙って取り消すことになります。
        """
        css = self.css().replace(" ", "")
        self.assertIn("--goal:#CC3311;", css)
        # 10色のどれとも重ならない ── 重なると「11色目」に見えます
        self.assertNotIn("#CC3311", [c.upper() for c in self.GIVEN])
        # 切れる口が残っていること。**これが「特別に許す」の根拠**
        graph = (Path(__file__).resolve().parent.parent / "app" / "templates"
                 / "graph.html").read_text(encoding="utf-8")
        self.assertIn('id="with-target"', graph)
        self.assertIn("目標線(45度線)", graph)

    def test_上から順に配る(self) -> None:
        """`cat-1` が1番目、`cat-2` が2番目 ── 並びが指定そのもの。"""
        self.assertEqual(dash.CAT_TONES[0], "cat-1")
        self.assertEqual(dash.CAT_TONES[-1], "cat-9")
        self.assertEqual(dash.CAT_REST, "cat-10")

    def test_暗い地でも地に沈まない(self) -> None:
        """**「その他」が黒い扇になって沈んでいた**のがここ。

        暗い地では3つ(indigo / green / wine)だけ明るさを持ち上げて
        います。残り7つは指定のまま ── 地に対して 3:1 を超えているので、
        触ると指定から離れるだけです。
        """
        import re

        css = self.css()
        # `data-theme="dark"` の側に並んでいる10色を読む
        dark = css[css.index('[data-theme="dark"]'):]
        found = dict(re.findall(r"--cat-(\d+):(#[0-9A-Fa-f]{6});", dark))
        self.assertEqual(len(found), 10, found)
        for n, value in found.items():
            with self.subTest(n=n):
                self.assertGreaterEqual(
                    contrast(value, "#161f24"), 3.0,
                    f"cat-{n} ({value}) が暗い地に沈みます")

    def test_明るい地でも地に沈まない(self) -> None:
        """淡い灰(その他)だけは薄いので、そこは扇の縁で見せます。"""
        for value in self.GIVEN[:-1]:
            with self.subTest(value=value):
                self.assertGreaterEqual(contrast(value, "#ffffff"), 1.5, value)

    def test_10色ともCSSに口がある(self) -> None:
        """サーバが `cat-N` と言っても、塗る側に口が無ければ色は出ません。"""
        css = (Path(__file__).resolve().parent.parent / "app" / "static"
               / "css" / "components.css").read_text(encoding="utf-8")
        for i in range(1, 11):
            with self.subTest(n=i):
                self.assertIn(f".mark-cat-{i}{{ fill:var(--cat-{i}); }}", css)
                self.assertIn(f".donut__ring.mark-cat-{i}", css)
                self.assertIn(f".dlg__swatch.mark-cat-{i}", css)

    def test_累積の線にも口がある(self) -> None:
        css = (Path(__file__).resolve().parent.parent / "app" / "static"
               / "css" / "components.css").read_text(encoding="utf-8")
        self.assertIn(".chart .line.mark-cumulative", css)
        self.assertIn(".dlg__swatch.mark-cumulative", css)


def contrast(a: str, b: str) -> float:
    """2色のコントラスト比(WCAG)。**色が地に沈んでいないかを見る。**"""
    def channels(value: str):
        value = value.lstrip("#")
        return [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def luminance(value: str) -> float:
        out = []
        for c in channels(value):
            out.append(c / 12.92 if c <= 0.03928
                       else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

    first, second = luminance(a), luminance(b)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


class ComboLegendTests(unittest.TestCase):
    """凡例の見本にも色が付くこと。

    線の見本は四角ではなく**横線**で描きます(棒と形で分けるため)。
    そのぶん色は `background` ではなく `border-top` に載るので、
    なかまごとに `color` を当てる行が要ります ── **1行忘れると、線
    そのものには色が付くのに凡例の見本だけ地の色**になり、凡例と線が
    結び付きません。
    """

    def css(self) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "static"
                / "css" / "components.css").read_text(encoding="utf-8")

    def test_線の見本に色を当てる行がある(self) -> None:
        css = self.css()
        for mark in ("qty", "dur", "stop", "goal", "cumulative"):
            with self.subTest(mark=mark):
                self.assertIn(f".dlg__swatch--line.mark-{mark}{{", css)


class StackedShiftBarTests(unittest.TestCase):
    """直枚数の棒を**直ごとに積み上げる**(VBA と同じ)。

    > 棒グラフが積み上げ棒グラフとして扱われてます？
    > 直枚数で積み上げならおかしくないでしょ？VBAではそうなってるが

    積み上げていませんでした。棒は1本で、値は**その日の全直合計** ──
    3直ぶんが入っているのに1本に潰れていて、題の「直枚数」とも
    食い違っていました(凡例だけが「その日の枚数」と正しかった)。
    """

    def points(self):
        from nippou.logic.packing_agg import day_points

        rows = []
        for day in ("2026年9月15日", "2026年9月16日"):
            for shift, count in (("1直", 30.0), ("2直", 40.0), ("3直", 50.0)):
                rows.append(ShiftAggregate(report_date=day, line="L-1",
                                           shift=shift, sheet_count=count))
        return day_points(rows)

    def tile(self):
        return dash.cumulative_tile(self.points(), "")

    def css(self, name: str) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "static"
                / "css" / name).read_text(encoding="utf-8")

    # -- 中身 -----------------------------------------------------------
    def test_直ごとの内訳を持つ(self) -> None:
        point = self.points()[0]
        self.assertEqual(point.quantity_by_shift,
                         {"1直": 30.0, "2直": 40.0, "3直": 50.0})
        # **合計は捨てません。** 累積も稼働率も合計から出しています
        self.assertEqual(point.quantity, 120.0)

    def test_棒は直ごとに1本ずつ(self) -> None:
        bars = [s for s in self.tile().series if s.kind == "bar"]
        self.assertEqual([s.title for s in bars], ["1直", "2直", "3直"])

    def test_下から1直2直3直の順(self) -> None:
        """**Excelと同じ 青・赤・緑。** 段は直の色そのものです。"""
        bars = [s for s in self.tile().series if s.kind == "bar"]
        self.assertEqual([s.mark for s in bars],
                         ["shift-1", "shift-2", "shift-3"])
        self.assertEqual(dash.SHIFT_STACK_ORDER[:3], ("1直", "2直", "3直"))

    def test_同じ積み名を持つ(self) -> None:
        bars = [s for s in self.tile().series if s.kind == "bar"]
        self.assertEqual({s.stack for s in bars}, {"day"})
        # 累積の線は積まない(右目盛りの別物)
        line = next(s for s in self.tile().series if s.key == "cumulative")
        self.assertEqual(line.stack, "")

    def test_積んだ高さは今までの棒と同じ(self) -> None:
        """**棒の高さは変わりません。** 中の切れ目が増えるだけです ──
        45度線に対するペースの読み方を変えないため。"""
        tile = self.tile()
        bars = [s for s in tile.series if s.kind == "bar"]
        for i, point in enumerate(self.points()):
            with self.subTest(day=point.work_date):
                self.assertAlmostEqual(sum(s.values[i] for s in bars),
                                       point.quantity)

    def test_出てこない直は段を作らない(self) -> None:
        """日勤だけのラインで「1直 0枚」の凡例が並ぶと、動いていない直が
        あるように読めます。"""
        from nippou.logic.packing_agg import day_points

        points = day_points([ShiftAggregate(report_date="2026年9月16日",
                                            line="L-1", shift="日勤",
                                            sheet_count=90.0)])
        bars = [s for s in dash.cumulative_tile(points, "").series
                if s.kind == "bar"]
        self.assertEqual([s.title for s in bars], ["日勤"])
        # **色は直で固定**で、「その日に出てきた順」ではありません ──
        # 日によって1直が青だったり赤だったりすると、並べて比べられません
        self.assertEqual(bars[0].mark, "shift-day")

    def test_内訳が無ければ合計1本に戻す(self) -> None:
        """古い集計を読んだとき。**棒を消しません** ── 合計だけでも
        絵は成り立ちます。"""
        from nippou.logic.packing_agg import DayPoint

        point = DayPoint(work_date="2026年9月16日", quantity=120.0,
                         cumulative_quantity=120.0)
        bars = [s for s in dash.cumulative_tile([point], "").series
                if s.kind == "bar"]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].values, [120.0])
        self.assertEqual(bars[0].stack, "")

    def test_画面へ積み名が渡る(self) -> None:
        bar = next(s for s in self.tile().series if s.kind == "bar")
        self.assertEqual(bar.as_dict()["stack"], "day")

    # -- 色 -------------------------------------------------------------
    def test_段の色はExcelと同じ青赤緑紫(self) -> None:
        """**2回とも「同じ分類だから色相は近くに」で失敗しました。**

        > んーその配色ほんとに守ってやってる？
        > こっちの提示した写真と全然違うじゃん
        > 同じカテゴリは似た色のルールは撤廃！
        > エクセルグラフの配色のようにやってよ

            緑の濃淡4段   隣の明度差が 1.4〜1.7 しかない
            10色を上から  indigo→cyan→teal で3つとも青〜緑の帯
                          (2直 cyan と 3直 teal は ΔE 34.9)

        Office の既定色(Excel のグラフがそのまま使う並び)を採ります。
        色相が4方向に散るので、隣り合っても見分けられます。
        """
        self.assertEqual(list(dash.SHIFT_QTY_CLASSES.values()),
                         ["shift-1", "shift-2", "shift-3", "shift-day"])
        css = self.css("tokens.css").replace(" ", "")
        for name, want in (("--shift-1", "#4F81BD"), ("--shift-2", "#C0504D"),
                           ("--shift-3", "#9BBB59"), ("--shift-day", "#8064A2")):
            with self.subTest(name=name):
                self.assertIn(f"{name}:{want};", css)
        # 前の2回のぶんは消えていること(残すと「どちらが本当か」が出ます)
        self.assertNotIn("--qty-1", css)
        self.assertNotIn("--shift-2:#3355cc", css)

    def test_タイルの種類色に負けない(self) -> None:
        """1クラスぶん(0,1,0)の `fill` は
        `.tile[data-tone] .chart .bar` (0,3,0) に負けます ── **段が
        全部なかまの色1色**になります(なりました)。
        """
        css = self.css("components.css")
        for mark in ("shift-1", "shift-2", "shift-3", "shift-day"):
            with self.subTest(mark=mark):
                self.assertIn(
                    f".tile[data-tone] .chart .bar.mark-{mark}"
                    f"{{ fill:var(--{mark}); stroke:var(--{mark}-edge); }}", css)
        for n in range(1, 11):
            with self.subTest(n=n):
                self.assertIn(
                    f".tile[data-tone] .chart .bar.mark-cat-{n}"
                    f"{{ fill:var(--cat-{n}); }}", css)

    def test_累積の線は段のどれとも違う色(self) -> None:
        """**同じ絵の中に同じ色を2つ出さない。**

        v3.44.0 で累積を teal(`#44AA99`)に置きました。棒を直ごとに
        積み上げて、そこが **3直の色**になりました ── 線は棒の上を
        横切るので、必ず重なります。10色の9番目 purple へ移しました。
        """
        css = self.css("tokens.css").replace(" ", "")
        self.assertIn("--cumulative:#AA4499;", css)
        self.assertNotIn("--cumulative:#44AA99;", css)
        # 段が使う4色のどれとも違うこと
        used = {"#4F81BD", "#C0504D", "#9BBB59", "#8064A2"}
        self.assertNotIn("#AA4499", used)

    def test_段のあいだに境を入れる(self) -> None:
        """棒が細い日(期間を長く取ると1本が数px)は、色の帯が隣り合う
        だけになります。**地の色の細い線で切っておきます。**"""
        self.assertIn(".chart .bar--seg{ stroke:var(--surface); stroke-width:1; }",
                      self.css("components.css"))
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "chart.js").read_text(encoding="utf-8")
        self.assertIn('s.stack ? " bar--seg" : ""', js)

    def test_目盛りの頭は積んだ高さで決める(self) -> None:
        """足さずに測ると、**棒が枠を突き抜けます。**"""
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "chart.js").read_text(encoding="utf-8")
        top = js[js.index("const topOf = (axis) => {"):]
        top = top[:top.index("\n  };")]
        self.assertIn("stacked", top)
        self.assertIn('s.kind === "bar" && s.stack', top)


class NeonSkinTests(WebTestCase):
    """ネオンの見た目と、その on/off                          [v3.51.0]

    > ネオンサイバーパンク風の鮮やかなビジュアルに変更してください
    > onoffができ修正前のものと切り替えれるようにしておいて
    > あくまで集計結果とグラフに関してだけです

    指定は Plotly (`template="plotly_dark"`)の形で受け取りましたが、
    **Plotly は入れません**(依存を足さないのが約束)。グラフは手書きの
    SVG なので、同じ指定を色と地の値に置き換えて当てています。
    """

    def css(self, name: str) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "static"
                / "css" / name).read_text(encoding="utf-8")

    # -- 外せること ----------------------------------------------------
    def test_全部が切り替えの中にある(self) -> None:
        """**外せば元の見た目に戻る。** 上書きの層の中だけに書きます ──
        外に1つでも書くと、`plain` にしても戻りきりません。"""
        for name in ("tokens.css", "components.css"):
            text = self.css(name)
            for line in text.splitlines():
                body = line.strip()
                if not body.startswith(("--neon-", "--glass-", "--chart-bg",
                                        "--glow")):
                    continue
                # 宣言はネオンの層の中にしかない
                before = text[:text.index(line)]
                self.assertIn('[data-skin="neon"]', before,
                              f"{name}: {body} が切り替えの外にあります")

    def test_前の姿を別に持たない(self) -> None:
        """`plain` 用の定義は**書きません**。上書きを外せば下の値が出ます
        ── 2つ持つと、片方だけ直す日が必ず来ます。"""
        for name in ("tokens.css", "components.css"):
            with self.subTest(name=name):
                self.assertNotIn('[data-skin="plain"]', self.css(name))

    def test_描く前に当てる(self) -> None:
        """あとから JS で付けると、一瞬だけ前の見た目が出ます(ちらつき)。"""
        base = (Path(__file__).resolve().parent.parent / "app" / "templates"
                / "base.html").read_text(encoding="utf-8")
        head = base[:base.index("</head>")]
        self.assertIn('data-skin="neon"', head)
        self.assertIn("nippou.skin", head)
        # 読めない場面(プライベート窓)でも落ちない
        self.assertIn("catch", head)

    def test_端末が覚える(self) -> None:
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "skin.js").read_text(encoding="utf-8")
        self.assertIn("localStorage", js)
        # 読み書きは必ず try/catch(プライベート窓では投げます)
        self.assertGreaterEqual(js.count("catch"), 3)

    def test_つまみが集計グラフの画面にある(self) -> None:
        html = self.client.get("/graph", headers=HEADERS).get_data(as_text=True)
        self.assertIn('id="neon-skin"', html)
        self.assertIn("ネオン表示", html)

    # -- 当たる範囲 ----------------------------------------------------
    def test_当たるのは集計とグラフだけ(self) -> None:
        """「あくまで集計結果とグラフに関してだけです」。

        地を暗くするのは `data-page` が `graph` / `agg` のときだけ。
        日報入力・GW計算・記録・設定はこれまでどおりです。
        """
        css = self.css("tokens.css")
        block = css[css.index('body[data-page="graph"]'):]
        block = block[:block.index("}")]
        self.assertIn('body[data-page="agg"]', css)
        for page in ("entry", "gw", "records", "settings"):
            with self.subTest(page=page):
                self.assertNotIn(f'data-page="{page}"', css)

    def test_画面が自分の名前を出している(self) -> None:
        """`data-page` が無いと、どの画面にも当たりません。"""
        for path, want in (("/", "entry"), ("/graph", "graph")):
            with self.subTest(path=path):
                html = self.client.get(path, headers=HEADERS).get_data(as_text=True)
                self.assertIn(f'<body data-page="{want}"', html)

    # -- 指定どおりか --------------------------------------------------
    def test_指定の色が入っている(self) -> None:
        css = self.css("tokens.css").replace(" ", "")
        for name, want in (
                ("--chart-bg", "#060a1e"),      # 深みのある濃紺
                ("--shift-1", "#6b2fd6"),       # 1直 紫・バイオレット
                ("--shift-2", "#38b6ff"),       # 2直 スカイブルー
                ("--shift-3", "#00e699"),       # 3直 ネオングリーン
                ("--cumulative", "#ff007f"),    # 累積 蛍光マゼンタ
                ("--goal", "#ff6600"),          # 目標 蛍光オレンジ
                ("--neon-qty", "#00ffcc"),
                ("--neon-dur", "#00bfff"),
                ("--neon-stop", "#ff6600")):
            with self.subTest(name=name):
                self.assertIn(f"{name}:{want};", css)

    def test_すりガラスのカード(self) -> None:
        css = self.css("tokens.css").replace(" ", "")
        self.assertIn("--glass-bg:rgba(15,23,42,.6);", css)
        self.assertIn("--glass-edge:rgba(255,255,255,.1);", css)
        self.assertIn("backdrop-filter:blur(8px)",
                      self.css("components.css").replace(" ", ""))

    def test_グリッドは主張させない(self) -> None:
        css = self.css("tokens.css").replace(" ", "")
        self.assertIn("--hairline:rgba(255,255,255,.1);", css)

    def test_棒と輪はほのかに光る(self) -> None:
        """色は塗りから採ります ── `drop-shadow` に色を書かないと
        `color` の値が使われるので、印ごとに `color` を置けば1本で足ります。
        """
        css = self.css("components.css").replace(" ", "")
        self.assertIn("filter:drop-shadow(00var(--glow)currentColor);", css)
        self.assertIn("--glow:6px;", self.css("tokens.css").replace(" ", ""))
        # 縁取りは光らせない(光ると縁の意味が消えます)
        self.assertIn(".line--halo{filter:none;}", css)

    def test_折れ線は太く点は大きく(self) -> None:
        """指定は「太さ 3〜4 / マーカー 8 前後」。半径は4(直径8)。"""
        css = self.css("components.css").replace(" ", "")
        self.assertIn(".chart.line{fill:none;stroke:var(--accent);stroke-width:3;}",
                      css)
        self.assertIn("stroke:var(--cumulative);stroke-width:4;", css)
        js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
              / "chart.js").read_text(encoding="utf-8")
        self.assertIn("const DOT_R = 4;", js)

    def test_Plotlyは入れない(self) -> None:
        """**依存を足さないのが約束。** 指定は Plotly の形で来ましたが、
        グラフは手書きの SVG のままです。"""
        root = Path(__file__).resolve().parent.parent
        req = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("plotly", req)
        for path in (root / "app" / "static" / "js").rglob("*.js"):
            with self.subTest(path=path.name):
                self.assertNotIn("plotly", path.read_text(encoding="utf-8").lower())
