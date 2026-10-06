"""45度線を画面まで通す ── グラフと設定

`tests/test_line_target.py` が CSV の読み方と線の形を守る。ここは
**そこから先**、タイルに乗るところと、画面から見えるところ。

守るのは:

    ・目標が無くても**実績のグラフは今までどおり出る**(止めない)
    ・累積の絵には第2軸の「目標値」、日別の枚数には横ばいの目標
    ・点の数はタイルごとに合う(線が途中で切れたりはみ出したりしない)
    ・グラフ画面のチェックで ON/OFF できる(VBA `Targetline` と同じ)
    ・設定画面から置き場所を変えられ、いま効いている値が読める
    ・取り込みは**管理者だけ**(共有の1ファイルを上書きするので)
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.models import DetailRecord, HeaderRecord
from nippou.presenters import dashboard as dash
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase

DAY = "2026年8月31日"


def tile(key: str, labels: list[str]) -> dash.Tile:
    return dash.Tile(key=key, title=key, kind=dash.KIND_LINE, labels=labels,
                     values=[0.0] * len(labels))


def combo(labels: list[str]) -> dash.Tile:
    return dash.Tile(
        key="trend_cumulative", title="推移", kind=dash.KIND_COMBO,
        labels=labels,
        series=[dash.Series(key="count", title="枚数", kind="bar",
                            axis="left", values=[1.0] * len(labels)),
                dash.Series(key="cumulative", title="累積", kind="line",
                            axis="right", values=[1.0] * len(labels))])


class AddTargetsTests(unittest.TestCase):
    """VBA `グラフ挿入` の「★目標値追加」と同じ足し方。"""

    def test_no_target_changes_nothing(self):
        tiles = [combo(["1", "2"]), tile("trend_count", ["1", "2"])]
        dash.add_targets(tiles, None)
        self.assertEqual(len(tiles[0].series), 2)
        self.assertEqual(tiles[1].target, [])

    def test_zero_target_changes_nothing(self):
        # VBA も `累積目標 <> 0` を見ていた
        tiles = [combo(["1", "2"])]
        dash.add_targets(tiles, 0)
        self.assertEqual(len(tiles[0].series), 2)

    def test_cumulative_gets_a_rising_series_on_the_right_axis(self):
        tiles = [combo(["1", "2", "3"])]
        dash.add_targets(tiles, 12)
        added = tiles[0].series[-1]
        self.assertEqual(added.key, "target")
        self.assertEqual(added.title, "目標値")
        # 累積と同じ目盛り。左に乗せると桁が2つ違って床に貼り付く
        self.assertEqual(added.axis, "right")
        self.assertEqual(added.values, [12.0, 24.0, 36.0])

    def test_count_gets_a_flat_target(self):
        tiles = [tile("trend_count", ["1", "2", "3"])]
        dash.add_targets(tiles, 12)
        self.assertEqual(tiles[0].target, [12.0, 12.0, 12.0])
        self.assertEqual(tiles[0].target_title, "目標値")

    def test_each_tile_uses_its_own_point_count(self):
        # 累積は日別の積み上げ、枚数の推移は保存のあった日だけ、と
        # 数が違うことがある。**片方の数で引かない**
        tiles = [combo(["1", "2", "3", "4"]), tile("trend_count", ["1", "2"])]
        dash.add_targets(tiles, 10)
        self.assertEqual(len(tiles[0].series[-1].values), 4)
        self.assertEqual(len(tiles[1].target), 2)

    def test_other_tiles_are_untouched(self):
        tiles = [tile("trend_weight", ["1", "2"]),
                 tile("trend_rate", ["1", "2"])]
        dash.add_targets(tiles, 12)
        # 目標は「枚数」の話。重量や稼働率に引いても意味が違う
        self.assertTrue(all(t.target == [] for t in tiles))

    def test_empty_tile_is_skipped(self):
        tiles = [tile("trend_count", [])]
        dash.add_targets(tiles, 12)
        self.assertEqual(tiles[0].target, [])

    def test_target_is_in_the_dict(self):
        tiles = [tile("trend_count", ["1"])]
        dash.add_targets(tiles, 12)
        body = tiles[0].as_dict()
        self.assertEqual(body["target"], [12.0])
        self.assertEqual(body["target_title"], "目標値")


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.db.schema import ensure_schema

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        conn = connect(Path(self._tmp.name) / "t.sqlite3")
        ensure_schema(conn)
        self.addCleanup(conn.close)
        self.repo = NippouRepository(conn)
        self.repo.save(
            HeaderRecord(report_date=DAY, line="L-1", shift="1直", page=1,
                         count="100", weight_kg="1000"),
            [DetailRecord(report_date=DAY, line="L-1", shift="1直", page=1,
                          row_no=1, lot="A", con="100", wei="1000",
                          s="0", th="30", tim="400")])

    def build(self, target=None):
        return dash.build(self.repo, report_date=DAY, line="L-1",
                          start=date(2026, 8, 1), end=date(2026, 8, 31),
                          target=target)

    def test_no_target_still_draws_the_actuals(self):
        body = self.build().as_dict()
        self.assertIsNone(body["target"])
        keys = [t["key"] for t in body["tiles"]]
        self.assertIn("trend_cumulative", keys)

    def test_target_text_has_no_decimal_point_for_whole_sheets(self):
        # 枚数に「12.0」は出さない。**紙で数えるものに小数は付かない**
        self.assertEqual(self.build(target=12.0).target_text, "12")
        self.assertEqual(self.build(target=1200).target_text, "1,200")
        self.assertEqual(self.build(target=12.5).target_text, "12.5")
        self.assertEqual(self.build().target_text, "")

    def test_target_reaches_the_tiles(self):
        body = self.build(target=12).as_dict()
        self.assertEqual(body["target"], 12)
        tiles = {t["key"]: t for t in body["tiles"]}
        series = [s["key"] for s in tiles["trend_cumulative"]["series"]]
        self.assertIn("target", series)
        self.assertTrue(tiles["trend_count"]["target"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class GraphScreenTests(WebTestCase):
    """グラフ画面。**ON/OFF はチェックで**(VBA `Targetline` と同じ)。"""

    def write_targets(self, text: str = "L-1,12\n") -> Path:
        from nippou.config import SETTINGS

        path = SETTINGS.line_target_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_checkbox_is_on_the_screen(self):
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn('id="with-target"', body)
        self.assertIn("目標線", body)

    def test_no_csv_says_so_and_points_at_the_settings(self):
        body = self.get("/graph").get_data(as_text=True)
        # **無いことを言う。** 黙って線が出ないと、設定し忘れに気づけない
        self.assertIn("/settings?tab=master", body)

    def test_target_is_used_when_the_csv_has_the_line(self):
        from nippou import work_context

        work_context.get_context().line = "L-1"
        self.write_targets()
        body = self.post("/api/graph/dashboard",
                         {"start": "2026-01-01", "end": "2030-12-31"}).get_json()
        self.assertEqual(body["target"], 12.0)

    def test_unchecking_drops_the_target(self):
        from nippou import work_context

        work_context.get_context().line = "L-1"
        self.write_targets()
        body = self.post("/api/graph/dashboard",
                         {"start": "2026-01-01", "end": "2030-12-31",
                          "with_target": False}).get_json()
        self.assertIsNone(body["target"])
        tiles = {t["key"]: t for t in body["tiles"]}
        series = [s["key"] for s in tiles["trend_cumulative"]["series"]]
        self.assertNotIn("target", series)

    def test_the_shift_end_review_also_gets_the_target(self):
        # 直の終わりに見るのはここ。「何枚だったか」だけでなく
        # 「届いたか」が同じ絵で読めないと、確認させる意味が薄い
        from nippou import work_context

        work_context.get_context().line = "L-1"
        self.write_targets()
        body = self.post("/api/graph/history",
                         {"start": "2026-01-01", "end": "2030-12-31"}).get_json()
        count = next(s for s in body["series"] if s["key"] == "count")
        self.assertEqual(len(count["target"]), len(count["values"]))
        self.assertTrue(all(v == 12.0 for v in count["target"]))
        # 重量と稼働率は枚数の話ではない。目標を重ねない
        for key in ("weight", "rate"):
            other = next(s for s in body["series"] if s["key"] == key)
            self.assertNotIn("target", other)

    def test_the_review_has_no_target_without_a_csv(self):
        body = self.post("/api/graph/history",
                         {"start": "2026-01-01", "end": "2030-12-31"}).get_json()
        count = next(s for s in body["series"] if s["key"] == "count")
        self.assertEqual(count["target"], [])

    def test_a_broken_csv_does_not_stop_the_graph(self):
        from nippou import work_context

        work_context.get_context().line = "L-1"
        self.write_targets("これは目標ではありません\n")
        res = self.post("/api/graph/dashboard",
                        {"start": "2026-01-01", "end": "2030-12-31"})
        # **読めなくても画面は出す。** 目標線が無いだけ
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.get_json()["target"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SettingsScreenTests(WebTestCase):
    def make_master(self) -> None:
        """参照用マスタの置き場所に「ライン毎目標」を持つマスタを置く。"""
        from nippou import config, user_settings
        from nippou.config import SETTINGS

        ref = self.tmp / "ref"
        ref.mkdir(parents=True, exist_ok=True)
        user_settings.save_many({config.KEY_REFERENCE_DIR: str(ref)})
        conn = sqlite3.connect(str(ref / SETTINGS.gw_material_master_filename))
        conn.execute('CREATE TABLE "ライン毎目標" '
                     '("管理番号" INTEGER, "ライン" TEXT, "目標" INTEGER)')
        conn.executemany('INSERT INTO "ライン毎目標" VALUES (?,?,?)',
                         [(1, "L-1", 12), (2, "機側", 100)])
        conn.commit()
        conn.close()

    # -- 設定できること ------------------------------------------------
    def test_置き場所はフォルダだけ出す(self):
        """**ファイル名は出さず、置き場所は出す。**

        「ファイル名までの入力は不要にしてください」── 名前は
        `ライン毎目標.csv` で固定です。ただし置き場所まで画面から
        消していたので、**参照用マスタと同じフォルダにしか置けません**
        でした(利用者の指摘:「CSVの読み込みはDBしかなくないですか？」)。
        目標はマスタとは別の人が別の周期で直すものなので、そこに縛る
        理由がありません。
        """
        body = self.get("/settings").get_data(as_text=True)
        # 欄はある(フォルダとして)
        self.assertIn('id="path-line_target_file"', body)
        self.assertIn("ライン毎目標の置き場所", body)
        # どのファイルを読んでいるかは、目標の面に出ている
        from nippou.config import SETTINGS

        self.assertIn(SETTINGS.line_target_filename, body)

    def test_読みに行く先の組に入っている(self):
        """置き場所は**それを読む機能と同じ考え方で並べる。**"""
        from nippou.presenters import settings as view
        from nippou import config

        read = dict((k, members) for k, _, members in view.PATH_GROUPS)["read"]
        self.assertIn(config.KEY_LINE_TARGET_FILE, read)

    def test_フォルダを渡せば名前はこちらで足す(self):
        """画面はフォルダしか出さないので、届くのもフォルダ。"""
        wanted = self.tmp / "共有" / "目標置き場"
        res = self.post("/api/settings/paths",
                        {"line_target_file": str(wanted), "password": "nisk"})
        self.assertEqual(res.status_code, 200)

        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.line_target_path,
                         wanted / "ライン毎目標.csv")

    def test_フォルダを変えても見本と読み込みがそこを使う(self):
        """**置き場所を変えたのに、見本は前の場所に出る**が起きない。"""
        wanted = self.tmp / "共有" / "目標置き場"
        self.post("/api/settings/paths",
                  {"line_target_file": str(wanted), "password": "nisk"})
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.post("/api/settings/line-targets/template", {}).get_json()
        self.assertEqual(Path(body["path"]).parent, wanted)

    def test_画面に出るのはフォルダのほう(self):
        """欄に `…\ライン毎目標.csv` まで出ると、名前まで打つ欄に見える。"""
        wanted = self.tmp / "共有" / "目標置き場"
        self.post("/api/settings/paths",
                  {"line_target_file": str(wanted), "password": "nisk"})

        from nippou.presenters import settings as view
        from nippou import config

        row = {p.key: p for p in view.path_views()}[config.KEY_LINE_TARGET_FILE]
        self.assertEqual(row.value, str(wanted))
        self.assertFalse(row.is_file, "フォルダを選ぶボタンが出ません")
        # 実際に見に行く道は、ファイルまで出す(そこが正)
        self.assertTrue(row.resolved.endswith("ライン毎目標.csv"))

    def test_the_card_is_on_the_screen(self):
        body = self.get("/settings").get_data(as_text=True)
        # グラフ画面がここへ飛ばしてくる。id を変えると行き先が切れる
        self.assertIn('id="line-targets"', body)
        self.assertIn("45度線", body)

    def test_saving_a_bare_name(self):
        res = self.post("/api/settings/paths",
                        {"line_target_file": "今期.csv", "password": "nisk"})
        self.assertEqual(res.status_code, 200)
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.line_target_path.name, "今期.csv")
        # 名前だけなら参照用マスタのフォルダの下
        self.assertEqual(SETTINGS.line_target_path.parent,
                         SETTINGS.gw_reference_dir)

    def test_saving_a_full_path(self):
        # **フォルダ付きも通す。** 共有の別のところに1つ置いて、
        # 全端末から読ませたいことがある
        wanted = self.tmp / "共有" / "目標.csv"
        res = self.post("/api/settings/paths",
                        {"line_target_file": str(wanted), "password": "nisk"})
        self.assertEqual(res.status_code, 200)
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.line_target_path, wanted)

    def test_a_wrong_extension_is_refused(self):
        """**拡張子が付いているものには名前を足しません。**

        足すと `目標.xlsx\ライン毎目標.csv` になり、打ち間違いが
        フォルダ名として通ってしまいます。拡張子が付いているものは
        ファイル名のつもりなので、違っていればここで断ります。
        """
        res = self.post("/api/settings/paths",
                        {"line_target_file": "目標.xlsx"})
        # 打ち間違いをそのまま保存すると、黙って読めないだけになる
        self.assertEqual(res.status_code, 400)
        self.assertIn(".csv", res.get_json()["error"]["message"])

    def test_changing_it_needs_the_admin_password(self):
        """**パスは全部守ります。**

        読むだけのCSVなので、はじめは守っていませんでした。ですが
        45度線は現場が目標として見ている線で、別のファイルを指した
        まま使うと「今日は届いている」と読めてしまいます。
        """
        res = self.post("/api/settings/paths", {"line_target_file": "x.csv"})
        self.assertEqual(res.status_code, 403)
        res = self.post("/api/settings/paths",
                        {"line_target_file": "x.csv", "password": "nisk"})
        self.assertEqual(res.status_code, 200)

    # -- いま効いている値が読めること ------------------------------------
    def test_the_values_show_on_the_screen(self):
        from nippou.config import SETTINGS

        SETTINGS.line_target_path.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.line_target_path.write_text("L-1,12\nLVC,119\n",
                                             encoding="utf-8")
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn("119", body)

    def test_unset_lines_are_listed_too(self):
        # 「出ていない」ことが見えないと、設定し忘れに気づけない
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn("未設定", body)

    def test_unreadable_lines_are_shown(self):
        from nippou.config import SETTINGS

        SETTINGS.line_target_path.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.line_target_path.write_text("L-1,12\nTOT,あとで\n",
                                             encoding="utf-8")
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn("読めなかった行があります", body)

    def test_a_missing_csv_is_not_a_red_problem(self):
        from nippou.presenters import settings as sp

        state = sp.to_dict()
        self.assertFalse(state["line_targets"]["exists"])
        # 置いていないだけで赤い印を出すと、本当の困りごとが埋もれる
        self.assertFalse(any("ライン毎目標" in p for p in state["problems"]))

    # -- 取り込み --------------------------------------------------------
    def test_import_needs_admin(self):
        res = self.post("/api/settings/line-targets/import")
        self.assertEqual(res.status_code, 403)

    def test_import_writes_the_csv(self):
        from nippou.config import SETTINGS

        self.make_master()
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/line-targets/import")
        self.assertEqual(res.status_code, 200)
        text = SETTINGS.line_target_path.read_text(encoding="utf-8")
        self.assertIn("L-1,12", text)
        # 機側 → 機側。**CSVはツールの名前で書く**
        self.assertIn("機側,100", text)

    def test_import_returns_the_whole_state(self):
        self.make_master()
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        body = self.post("/api/settings/line-targets/import").get_json()
        self.assertIn("line_targets", body)
        self.assertEqual(body["line_targets"]["count"], 2)

    def test_import_without_a_master_says_so(self):
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/settings/line-targets/import")
        self.assertEqual(res.status_code, 502)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class TemplateTests(WebTestCase):
    """見本を出す ── **これを書き換えれば済む。**

    「見本出力を作ってください。それ書き換えたら済むし」。目標CSVは
    メモ帳で直せる形なので、**全ラインぶんの行**さえ出しておけば、
    数字を書き換えて保存するだけで終わります。
    """

    def admin(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})

    def test_管理者でなければ断る(self) -> None:
        res = self.post("/api/settings/line-targets/template", {})
        self.assertEqual(res.status_code, 403)

    def test_全ラインぶんの行が出る(self) -> None:
        from nippou import constants

        self.admin()
        body = self.post("/api/settings/line-targets/template", {}).get_json()
        text = Path(body["path"]).read_text(encoding="utf-8-sig")
        for name in constants.LINE_NAMES:
            with self.subTest(line=name):
                self.assertIn(f"{name},", text)
        # 書き換え方も一緒に書いておく(メモ帳で開く人しか居ない)
        self.assertIn("#", text)

    def test_出した見本はそのまま読み戻せる(self) -> None:
        """**出したものが読めなければ見本ではありません。**"""
        from nippou.logic import line_target

        self.admin()
        body = self.post("/api/settings/line-targets/template", {}).get_json()
        found = line_target.parse(
            Path(body["path"]).read_text(encoding="utf-8-sig"))
        self.assertEqual([p for p in found.problems if p.line_no], [])

    def test_すでにあるファイルは上書きしない(self) -> None:
        """**手で入れた目標が消えます。** 隣に見本を作って見てもらう。"""
        from nippou.config import SETTINGS

        target = SETTINGS.line_target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("L-1,999\n", encoding="utf-8")
        self.admin()
        body = self.post("/api/settings/line-targets/template", {}).get_json()
        self.assertNotEqual(body["path"], str(target))
        self.assertEqual(target.read_text(encoding="utf-8"), "L-1,999\n")
        self.assertIn("見本", Path(body["path"]).name)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ReadBackTests(WebTestCase):
    """**書き換えたCSVを、そのまま効かせる。**

    利用者の言葉:「マスタから読み取りしかないけど CSVから読む は
    必要ですよ(見本を出す意味がないよ)」── そのとおりでした。
    見本は、いまのファイルがあると隣に `.見本.csv` として出ます。
    書き換えても**それを本物にする道が無い**ので、見本を出しても
    一周しませんでした。

        見本を出す → 数字を書き換える → ここで渡す → 効く
    """

    def admin(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})

    def send(self, text: str, *, name: str = "目標.csv", apply: bool = False,
             encoding: str = "utf-8-sig"):
        import io

        data = {"file": (io.BytesIO(text.encode(encoding)), name)}
        if apply:
            data["apply"] = "1"
        return self.client.post("/api/settings/line-targets/upload",
                                data=data, headers=HEADERS,
                                content_type="multipart/form-data")

    def target_path(self):
        from nippou.config import SETTINGS

        return SETTINGS.line_target_path

    # -- 鍵 ------------------------------------------------------------
    def test_管理者でなければ断る(self) -> None:
        """共有に置いた1つのファイルを書き換えます(全端末に効きます)。"""
        res = self.send("L-1,12\n")
        self.assertEqual(res.status_code, 403)

    # -- 下見 ----------------------------------------------------------
    def test_渡しただけでは書かない(self) -> None:
        """**押す前に、何が入るのかを全部出す。**"""
        self.admin()
        body = self.send("L-1,12\nLVC,119\n").get_json()
        self.assertFalse(body["applied"])
        self.assertEqual([(r["line"], r["text"]) for r in body["rows"]],
                         [("L-1", "12"), ("LVC", "119")])
        self.assertFalse(self.target_path().exists(), "下見なのに書いています")

    def test_読めなかった行も出す(self) -> None:
        """黙って飛ばすと、打ち間違いに気づけません。"""
        self.admin()
        body = self.send("L-1,12\nこれは行ではありません\n").get_json()
        self.assertTrue(body["problems"])
        self.assertEqual(len(body["rows"]), 1)

    # -- 決定 ----------------------------------------------------------
    def test_決定で効く(self) -> None:
        from nippou.services import targets as targets_service

        self.admin()
        body = self.send("L-1,12\nLVC,119\n", apply=True).get_json()
        self.assertTrue(body["applied"])
        self.assertEqual(targets_service.of("L-1"), 12)
        self.assertEqual(targets_service.of("LVC"), 119)

    def test_決定したら画面の表も入れ替わる(self) -> None:
        """「入った」だけでは確かめられません。"""
        self.admin()
        body = self.send("L-1,12\n", apply=True).get_json()
        rows = {r["line"]: r["text"] for r in body["targets"]["rows"]}
        self.assertEqual(rows["L-1"], "12")

    def test_見本を書き換えて渡せば一周する(self) -> None:
        """**ここが通らないと、見本を出す意味がありません。**"""
        self.admin()
        made = self.post("/api/settings/line-targets/template", {}).get_json()
        text = Path(made["path"]).read_text(encoding="utf-8-sig")
        text = text.replace("L-1,0", "L-1,777")
        res = self.send(text, name="目標.見本.csv", apply=True)
        self.assertEqual(res.status_code, 200)

        from nippou.services import targets as targets_service
        self.assertEqual(targets_service.of("L-1"), 777)

    # -- 読めないものを渡したとき ----------------------------------------
    def test_空のファイルは断る(self) -> None:
        self.admin()
        res = self.send("")
        self.assertEqual(res.status_code, 422)

    def test_読めないCSVでいまの目標を消さない(self) -> None:
        """**読めないファイルのせいで、効いている目標が消えない。**"""
        self.admin()
        self.send("L-1,12\n", apply=True)
        res = self.send("なにも,読めません\nこれも\n", apply=True)
        self.assertEqual(res.status_code, 422)

        from nippou.services import targets as targets_service
        self.assertEqual(targets_service.of("L-1"), 12)

    def test_Excelから出たShift_JISも読める(self) -> None:
        """現場のCSVは Excel 由来の cp932 のことがあります。"""
        self.admin()
        res = self.send("# ライン毎目標\nL1,55\n", apply=True,
                        encoding="cp932")
        self.assertEqual(res.status_code, 200)

        from nippou.services import targets as targets_service
        self.assertEqual(targets_service.of("L-1"), 55)

    def test_ファイルを選んでいなければ断る(self) -> None:
        self.admin()
        res = self.client.post("/api/settings/line-targets/upload",
                               data={}, headers=HEADERS,
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 400)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ReloadTests(WebTestCase):
    """メモ帳で直したあと、**効いたかどうかをその場で確かめる。**

    グラフを開けばどのみち読み直されますが、そこまで行かないと
    分からないのでは「直したつもり」で終わります。
    """

    def test_鍵は要らない(self) -> None:
        """読むだけで、何も書きません。"""
        res = self.post("/api/settings/line-targets/reload", {})
        self.assertEqual(res.status_code, 200)

    def test_直した値がその場で読める(self) -> None:
        from nippou.config import SETTINGS

        target = SETTINGS.line_target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("L-1,321\n", encoding="utf-8")
        body = self.post("/api/settings/line-targets/reload", {}).get_json()
        rows = {r["line"]: r["text"] for r in body["targets"]["rows"]}
        self.assertEqual(rows["L-1"], "321")
        self.assertIn("読み直しました", body["message"])

    def test_まだ無いことも言う(self) -> None:
        """**黙って空を返さない。** 置き忘れに気づける。"""
        body = self.post("/api/settings/line-targets/reload", {}).get_json()
        self.assertFalse(body["targets"]["exists"])
        self.assertIn("まだありません", body["message"])
