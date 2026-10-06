"""停止内訳をCSVで持つ (v3.93.0)

> 伝送用ファイルで読み込んでいる 作業停止時間内訳_1/_2/_3 を ライン毎目標と
> 同じようにCSVでも読めるようにし、CSVとマスタがあればCSV、CSVがなければ
> マスタを見る … 停止内訳も管理者以外が触れるようにしたいためです

守るのは:

    ・CSVに行がある分類は**CSVだけ**(表と重ねない ── 消した理由が戻らない)
    ・CSVに無い分類は表のまま / CSVが無い・読めないときは表
    ・CSVが3つとも持っていれば、表を写しに行かない
    ・読めない行は捨てずに出す(行番号つき)。記号の重複は後ろを使わない
    ・停止理由を読むところ(日報入力の選択欄・保存前チェック・集計の名前)に効く
    ・参照設定に「停止内訳の置き場所」(フォルダだけ。名前は 停止内訳.csv)
    ・マスタの「停止内訳」の面: 見本を出す・渡して効かせる・読み直す は
      **鍵なし**で押せる。渡しただけでは書かない。前のファイルを残す
"""
from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import stop_csv                               # noqa: E402
from tests._web import HAS_FLASK, HEADERS, SKIP_REASON, WebTestCase  # noqa: E402

SAMPLE = """# 覚え書き
分類,内訳番号,内訳,備考
1,0,休憩食事,
1,1,TPM活動・清掃,
2,イ,突発停止(機械),
3,A,段取り変更,
"""


# ==================================================================
# 読み方(純ロジック)
# ==================================================================
class ParseTests(unittest.TestCase):
    def test_reads_by_category_in_file_order(self):
        found = stop_csv.parse(SAMPLE)
        self.assertEqual(found.problems, [])
        self.assertEqual([(r.code, r.label) for r in found.of("設備停止")],
                         [("0", "休憩食事"), ("1", "TPM活動・清掃")])
        self.assertEqual([r.code for r in found.of("不稼働")], ["イ"])
        self.assertEqual([r.code for r in found.of("ハンドリング")], ["A"])
        self.assertEqual(found.count, 4)

    def test_category_can_be_written_many_ways(self):
        for text, want in (("1", "設備停止"), ("１", "設備停止"),
                           ("作業停止時間内訳_2", "不稼働"), ("_3", "ハンドリング"),
                           ("管理ロス", "設備停止"), ("突発・待ち", "不稼働"),
                           ("設備停止", "設備停止"), ("ハンドリング", "ハンドリング")):
            with self.subTest(text=text):
                self.assertEqual(stop_csv.category_of(text), want)
        self.assertIsNone(stop_csv.category_of("4"))

    def test_code_is_kept_as_written(self):
        """**記号は書いたとおり。** 日報に残っている記号と1文字でも違うと、
        昔の日報の停止が「内訳にない記号」になる(半角の片仮名も変えない)。"""
        found = stop_csv.parse("2,ｲ,半角の記号\n")
        self.assertEqual(found.of("不稼働")[0].code, "ｲ")

    def test_tabs_quotes_and_excel_blank_rows(self):
        found = stop_csv.parse('1\t0\t休憩食事\n1,1,"TPM,清掃",メモ\n,,,\n')
        self.assertEqual(found.problems, [])
        self.assertEqual([r.label for r in found.of("設備停止")],
                         ["休憩食事", "TPM,清掃"])
        self.assertEqual(found.of("設備停止")[1].note, "メモ")

    def test_bad_rows_are_reported_with_line_numbers(self):
        found = stop_csv.parse("1,0\nx,Z,知らない分類\n1,,記号なし\n1,5,\n")
        self.assertEqual(found.count, 0)
        self.assertEqual([p.line_no for p in found.problems], [1, 2, 3, 4])
        self.assertTrue(all(p.level == stop_csv.SKIP for p in found.problems))
        self.assertIn("分類「x」", found.problems[1].reason)

    def test_duplicate_code_keeps_the_first(self):
        found = stop_csv.parse("3,A,段取り変更\n3,A,別の名前\n")
        self.assertEqual([r.label for r in found.of("ハンドリング")], ["段取り変更"])
        self.assertEqual(found.problems[0].line_no, 2)
        self.assertIn("1行目", found.problems[0].reason)

    def test_code_of_the_wrong_kind_is_used_but_warned(self):
        """集計は記号の文字で分ける(VBA `CheckCharType`)ので、言う。"""
        found = stop_csv.parse("2,5,数字の記号\n")
        self.assertEqual([r.code for r in found.of("不稼働")], ["5"])
        self.assertEqual(found.problems[0].level, stop_csv.WARN)
        self.assertIn("管理ロス", found.problems[0].reason)


class PickTests(unittest.TestCase):
    MASTER = {
        "設備停止": [stop_csv.Reason("0", "休憩食事"), stop_csv.Reason("2", "人員不足")],
        "不稼働": [stop_csv.Reason("イ", "突発停止(機械)")],
        "ハンドリング": [stop_csv.Reason("A", "段取り変更")],
    }

    def test_csv_category_replaces_the_table_without_merging(self):
        """**重ねない。** CSVから消した「人員不足」が表から戻ってこない。"""
        picked = stop_csv.pick(stop_csv.parse("1,0,休憩食事\n"), self.MASTER)
        self.assertEqual([r.code for r in picked.values["設備停止"]], ["0"])
        self.assertEqual(picked.origins["設備停止"], stop_csv.FROM_CSV)

    def test_categories_missing_from_csv_stay_on_the_table(self):
        picked = stop_csv.pick(stop_csv.parse("1,0,休憩食事\n"), self.MASTER)
        self.assertEqual(picked.origins["不稼働"], stop_csv.FROM_MASTER)
        self.assertEqual([r.code for r in picked.values["ハンドリング"]], ["A"])

    def test_no_csv_means_table(self):
        picked = stop_csv.pick(stop_csv.Parsed(), self.MASTER)
        self.assertEqual(set(picked.origins.values()), {stop_csv.FROM_MASTER})


class WriteTests(unittest.TestCase):
    def test_round_trip(self):
        found = stop_csv.parse(SAMPLE + '3,B,"内径,カット",メモ\n')
        again = stop_csv.parse(stop_csv.as_csv(found.values))
        self.assertEqual(again.problems, [])
        self.assertEqual(again.values, found.values)

    def test_written_file_explains_itself(self):
        text = stop_csv.as_csv({})
        self.assertIn("分類,内訳番号,内訳,備考", text)
        self.assertIn("1=管理ロス", text)
        self.assertIn("\r\n", text)                 # メモ帳で崩れない

    def test_diff_lists_added_removed_renamed_and_moved(self):
        before = stop_csv.parse("1,0,休憩食事\n1,1,TPM\n2,イ,突発\n").values
        after = stop_csv.parse("1,0,休憩\n2,イ,突発\n2,ロ,待ち\n3,1,TPM\n").values
        d = stop_csv.diff(before, after)
        self.assertEqual([x["code"] for x in d["added"]], ["ロ"])
        self.assertEqual(d["removed"], [])
        self.assertEqual([x["code"] for x in d["renamed"]], ["0"])
        self.assertEqual([x["code"] for x in d["moved"]], ["1"])


# ==================================================================
# 伝送用ファイルの表と合わせる(stop_master)
# ==================================================================
def make_transmission(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for no, rows in ((1, [("休憩食事", "0"), ("人員不足", "2")]),
                         (2, [("突発停止(機械)", "イ")]),
                         (3, [("段取り変更", "A")])):
            conn.execute(f'CREATE TABLE "作業停止時間内訳_{no}" '
                         '("管理番号" INTEGER, "内訳" TEXT, "内訳番号" TEXT, "備考" TEXT)')
            conn.executemany(f'INSERT INTO "作業停止時間内訳_{no}" VALUES (?,?,?,?)',
                             [(i, label, code, "") for i, (label, code) in enumerate(rows, 1)])
        conn.commit()
    finally:
        conn.close()


class LoaderTests(unittest.TestCase):
    """`stop_master.load_all_categories` ── 停止理由を読むところは全部ここを通る。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.master = root / "ref" / "伝送用ファイル.sqlite3"
        self.csv = root / "ref" / "停止内訳.csv"
        make_transmission(self.master)
        (root / "cache").mkdir()
        from nippou import source_db
        from unittest import mock
        patcher = mock.patch.object(source_db, "copy_dir",
                                    return_value=root / "cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        source_db.forget()
        self.addCleanup(source_db.forget)

    def load(self):
        from nippou.access_bridge import stop_master
        return stop_master.load_all_categories(self.master, csv_path=self.csv)

    def test_no_csv_reads_the_table(self):
        got = self.load()
        self.assertEqual([r.code for r in got["設備停止"]], ["0", "2"])

    def test_csv_wins_per_category(self):
        self.csv.write_text("1,0,休憩\n", encoding="utf-8-sig")
        got = self.load()
        self.assertEqual([(r.code, r.label) for r in got["設備停止"]], [("0", "休憩")])
        self.assertEqual([r.code for r in got["不稼働"]], ["イ"])

    def test_table_is_not_read_when_csv_covers_everything(self):
        from unittest import mock

        from nippou.access_bridge import stop_master
        self.csv.write_text(SAMPLE, encoding="utf-8")
        with mock.patch.object(stop_master, "import_table",
                               side_effect=AssertionError("表を読んだ")):
            got = self.load()
        self.assertEqual(sum(len(v) for v in got.values()), 4)

    def test_shift_jis_from_notepad_is_read(self):
        self.csv.write_bytes("3,B,内径カット\n".encode("cp932"))
        got = self.load()
        self.assertEqual([r.label for r in got["ハンドリング"]], ["内径カット"])

    def test_unreadable_csv_falls_back_to_the_table(self):
        self.csv.mkdir(parents=True)                 # 名前はあるがファイルではない
        got = self.load()
        self.assertEqual([r.code for r in got["設備停止"]], ["0", "2"])

    def test_sources_show_both_and_origins(self):
        from nippou.access_bridge import stop_master
        self.csv.write_text("1,0,休憩\n", encoding="utf-8")
        src = stop_master.load_sources(self.master, csv_path=self.csv,
                                       read_master=True)
        self.assertEqual(src.origins["設備停止"], stop_csv.FROM_CSV)
        self.assertEqual(src.origins["不稼働"], stop_csv.FROM_MASTER)
        self.assertEqual(len(src.master["設備停止"]), 2)   # 比べて見せるため読む
        self.assertTrue(src.csv_exists)


# ==================================================================
# 画面と、停止理由を読むところ
# ==================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    def setUp(self):
        super().setUp()
        make_transmission(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.csv = self.tmp / "ref" / "停止内訳.csv"

    def upload(self, text: str, *, apply: bool = False, encoding="utf-8-sig"):
        data = {"file": (io.BytesIO(text.encode(encoding)), "停止内訳.見本.csv")}
        if apply:
            data["apply"] = "1"
        return self.client.post("/api/settings/stop-reasons/upload", data=data,
                                headers=HEADERS, content_type="multipart/form-data")

    # ---- 読むところに効く -------------------------------------------
    def test_entry_choices_and_save_check_use_the_csv(self):
        self.csv.write_text("1,0,休憩\n1,9,新しい理由\n", encoding="utf-8-sig")
        from nippou.presenters import entry
        from nippou.services import shift_check

        choices = entry.stop_choices()
        first = next(g for g in choices if g["category"] == "設備停止")
        self.assertEqual([r["code"] for r in first["reasons"]], ["0", "9"])
        codes = shift_check.known_stop_codes()
        self.assertIn("9", codes)
        self.assertNotIn("2", codes)               # CSVから消した理由は戻らない
        self.assertIn("イ", codes)                 # CSVに無い分類は表のまま

    def test_entry_screen_lists_the_csv_reason(self):
        self.csv.write_text("1,9,CSVで足した理由\n", encoding="utf-8-sig")
        self.assertIn("CSVで足した理由", self.get("/").get_data(as_text=True))

    # ---- 参照設定 ----------------------------------------------------
    def test_path_field_is_a_folder_with_a_fixed_name(self):
        from nippou import config
        from nippou.config import SETTINGS
        from nippou.presenters import settings as view

        self.assertEqual(SETTINGS.stop_reason_csv_path, self.csv)
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn(f'id="path-{config.KEY_STOP_REASON_FILE}"', html)
        self.assertIn("停止内訳の置き場所", html)
        row = {p.key: p for p in view.path_views()}[config.KEY_STOP_REASON_FILE]
        self.assertEqual(row.looking_for, "ファイル")
        read_group = next(g for g in view.PATH_GROUPS if g[0] == "read")
        self.assertIn(config.KEY_STOP_REASON_FILE, read_group[2])
        # 無くても困りごとにしない(表を読む)
        self.assertFalse(any("停止内訳" in p for p in view.to_dict()["problems"]))

    def test_changing_the_folder_needs_the_password_and_appends_the_name(self):
        from nippou.config import SETTINGS
        wanted = self.tmp / "現場"
        wanted.mkdir()
        res = self.post("/api/settings/paths", {"stop_reason_file": str(wanted)})
        self.assertEqual(res.status_code, 403)
        res = self.post("/api/settings/paths",
                        {"stop_reason_file": str(wanted), "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(SETTINGS.stop_reason_csv_path, wanted / "停止内訳.csv")

    def test_goes_into_distribution(self):
        from nippou import config, distribution
        self.assertIn(config.KEY_STOP_REASON_FILE, distribution.ITEM_KEYS)

    # ---- マスタの「停止内訳」の面(鍵なし) ------------------------------
    def test_tab_is_shown_without_the_key(self):
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="panel-master-stops"', html)
        self.assertIn("見本を出す(これを書き換えれば済みます)", html)
        self.assertIn("鍵(管理者パスワード)は要りません", html)
        # 表の中身がそのまま出る
        self.assertIn("人員不足", html)

    def test_template_without_the_key_writes_current_list(self):
        res = self.post("/api/settings/stop-reasons/template")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(self.csv.is_file())
        found = stop_csv.parse(self.csv.read_text(encoding="utf-8-sig"))
        self.assertEqual([r.code for r in found.of("設備停止")], ["0", "2"])
        self.assertTrue(self.csv.read_bytes().startswith(b"\xef\xbb\xbf"))  # BOM

    def test_template_never_overwrites(self):
        self.csv.write_text("1,0,手で直した\n", encoding="utf-8-sig")
        res = self.post("/api/settings/stop-reasons/template")
        self.assertEqual(res.status_code, 200)
        self.assertIn("手で直した", self.csv.read_text(encoding="utf-8-sig"))
        sample = self.csv.with_suffix(".見本.csv")
        self.assertIn("手で直した", sample.read_text(encoding="utf-8-sig"))

    def test_upload_preview_writes_nothing(self):
        res = self.upload("1,0,休憩\n1,9,新しい\n")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertFalse(body["applied"])
        self.assertFalse(self.csv.exists())
        self.assertEqual([d["code"] for d in body["diff"]["added"]], ["9"])
        self.assertEqual([d["code"] for d in body["diff"]["removed"]], ["2"])
        self.assertEqual([d["code"] for d in body["diff"]["renamed"]], ["0"])
        groups = {g["category"]: g for g in body["groups"]}
        self.assertTrue(groups["不稼働"]["from_master"])

    def test_upload_apply_without_the_key_and_keeps_the_previous_file(self):
        self.csv.write_text("1,0,前の中身\n", encoding="utf-8-sig")
        res = self.upload("1,0,休憩\n2,ロ,待ち\n", apply=True, encoding="cp932")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertTrue(body["applied"])
        found = stop_csv.parse(self.csv.read_text(encoding="utf-8-sig"))
        self.assertEqual([r.label for r in found.of("不稼働")], ["待ち"])
        previous = self.csv.with_suffix(".前回.csv")
        self.assertIn("前の中身", previous.read_text(encoding="utf-8-sig"))
        origins = {g["category"]: g["origin"] for g in body["reasons"]["groups"]}
        self.assertEqual(origins["設備停止"], "CSV")
        self.assertEqual(origins["ハンドリング"], "マスタ")

    def test_unreadable_upload_keeps_the_current_file(self):
        self.csv.write_text("1,0,いまの中身\n", encoding="utf-8-sig")
        res = self.upload("ライン,目標\nL1,12\n", apply=True)
        self.assertEqual(res.status_code, 422)
        self.assertIn("いまの中身", self.csv.read_text(encoding="utf-8-sig"))

    def test_reload_reports_rows_that_were_not_used(self):
        self.csv.write_text("1,0,休憩\n9,X,知らない分類\n", encoding="utf-8-sig")
        res = self.post("/api/settings/stop-reasons/reload")
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["reasons"]["problems"][0]["line_no"], 2)
        self.assertEqual(body["reasons"]["csv_count"], 1)

    def test_table_note_says_the_csv_wins(self):
        from nippou.master_admin import TABLE_NOTES
        for no in (1, 2, 3):
            with self.subTest(no=no):
                self.assertIn("停止内訳.csv", TABLE_NOTES[f"作業停止時間内訳_{no}"])


if __name__ == "__main__":
    unittest.main()
