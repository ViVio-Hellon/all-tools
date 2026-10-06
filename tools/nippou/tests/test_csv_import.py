"""過去の日報をCSVから取り込む ── **出したものが、そのまま戻ること**

【このテストの本題】
取り込みが正しいかどうかは、**書き出して読み戻したときに同じになるか**で
決まります。列の写し戻しを1つ間違えると、納入先の欄に包装仕様書Noが
入ったまま静かに通ります ── 数は合っているので、誰も気づきません。

だから往復を縛ります。打つ → 書き出す → 消す → 読み戻す → 同じ。

【押すまで書かないこと】
取り込みは、いま入っているものを黙って置き換えられる操作です。
下見(`preview`)が1行も書かないこと、置き換わるページを数ではなく
**どれかで**言うことも、ここで確かめます。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.logic import csv_import
from nippou.reporting import csv_export
from nippou.services import csv_import as import_service

DAY = "2026年8月3日"


def row(**values) -> DetailRecord:
    base = dict(report_date=DAY, line="L-1", shift="1直", page=1, row_no=1)
    base.update(values)
    return DetailRecord(**base)


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = connect(self.tmp / "t.sqlite3")
        self.repo = NippouRepository(self.conn)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.conn.close)

    def save(self, *details: DetailRecord, **head) -> None:
        base = dict(report_date=DAY, line="L-1", shift="1直", page=1,
                    worker="山田 田中", day_shift="無")
        base.update(head)
        self.repo.save(HeaderRecord(**base), list(details))

    def export(self) -> Path:
        """いまのDBを明細CSVへ。取り込みの入力にします。"""
        out = self.tmp / "明細.csv"
        csv_export.write_daily_detail_csv(self.repo, DAY, "L-1", out)
        return out

    def wipe(self) -> None:
        self.conn.execute("DELETE FROM daily_detail")
        self.conn.execute("DELETE FROM daily_header")
        self.conn.execute("DELETE FROM packing_report")
        self.conn.commit()


class RoundTripTests(RepoTestCase):
    """**出したものが、そのまま戻る。** ここが本題。"""

    def full_row(self) -> DetailRecord:
        return row(lot="H5422S0", zai="F52S", siz="1.0x1000x2000", ken="9",
                   kz="08", kh="00", sz="10", sh="30", hit="1", ai="有",
                   mai="50", tut="2", vc="両面", et="反転",
                   s="1", th="30", ss="イ", ths="20", sth="A", tht="10",
                   con="100", wei="1200", tim="84", uni="12",
                   others1="H283", others2="JISNﾌﾗﾂﾄ", others3="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                   others4="1P1186", others5="縦", others6="横",
                   keisu="3", hiki_no="60717001")

    def test_打った欄が全部戻る(self) -> None:
        """**1欄でも取り違えると、数は合ったまま中身が入れ替わる。**"""
        original = self.full_row()
        self.save(original)
        path = self.export()
        self.wipe()

        found = csv_import.parse_file(path)
        self.assertTrue(found.ok, found.problems)
        back = found.pages[0].details[0]
        for name in ("lot", "zai", "siz", "ken", "kz", "kh", "sz", "sh",
                     "hit", "ai", "mai", "tut", "vc", "et",
                     "s", "th", "ss", "ths", "sth", "tht",
                     "con", "wei", "tim", "uni",
                     "others1", "others2", "others3", "others4",
                     "others5", "others6", "keisu", "hiki_no"):
            with self.subTest(field=name):
                self.assertEqual(getattr(back, name),
                                 getattr(original, name), name)

    def test_鍵も戻る(self) -> None:
        self.save(self.full_row())
        found = csv_import.parse_file(self.export())
        header = found.pages[0].header
        self.assertEqual((header.report_date, header.line, header.shift,
                          header.page), (DAY, "L-1", "1直", 1))
        self.assertEqual(found.pages[0].details[0].row_no, 1)

    def test_直に1つのものも戻る(self) -> None:
        self.save(self.full_row())
        header = csv_import.parse_file(self.export()).pages[0].header
        self.assertEqual(header.worker, "山田 田中")
        self.assertEqual(header.day_shift, "無")

    def test_行ごとの理由が行へ戻る(self) -> None:
        """CSVには `3行目: …` の形で1つ入っています。**割り戻します。**

        まとめたままヘッダに置くと、次に保存したときに行から作り直されて
        消えます。
        """
        self.save(row(row_no=1, lot="A", th="30", s="ヨ", reason="棚卸し準備"),
                  row(row_no=3, lot="B", th="20", s="ヨ", reason="点検表の差し替え"),
                  reason="1行目: 棚卸し準備 / 3行目: 点検表の差し替え")
        found = csv_import.parse_file(self.export())
        by_row = {d.row_no: d.reason for d in found.pages[0].details}
        self.assertEqual(by_row[1], "棚卸し準備")
        self.assertEqual(by_row[3], "点検表の差し替え")

    def test_ページも直も混ざらない(self) -> None:
        self.save(row(lot="A"), page=1)
        self.save(row(page=2, lot="B"), page=2)
        self.save(row(shift="2直", lot="C"), shift="2直")
        found = csv_import.parse_file(self.export())
        self.assertEqual([p.label for p in found.pages],
                         [f"{DAY} L-1 1直 1ページ", f"{DAY} L-1 1直 2ページ",
                          f"{DAY} L-1 2直 1ページ"])  # 画面は正規の呼び名(v4.12.5)

    def test_入れ直すと集計まで同じになる(self) -> None:
        """**紙もグラフも集計から出ます。** 明細だけ戻しても足りません。"""
        self.save(row(lot="A", con="100", wei="1200", tim="84", s="1", th="30"))
        from nippou.services import summary

        summary.refresh_shift(self.repo, DAY, "L-1", "1直")
        before = summary.day_rows(self.repo, DAY, "L-1")[0]

        path = self.export()
        self.wipe()
        _preview, result = import_service.apply(self.repo, path)
        self.assertEqual(result.pages, 1)
        self.assertEqual(result.summaries, 1)

        after = summary.day_rows(self.repo, DAY, "L-1")[0]
        self.assertEqual(after.sheet_count, before.sheet_count)
        self.assertEqual(after.weight_kg, before.weight_kg)
        self.assertEqual(after.total_stop_minutes, before.total_stop_minutes)
        self.assertEqual(after.operating_rate_pct, before.operating_rate_pct)

    def test_月別に書き出したものも読める(self) -> None:
        """**控えとして使えます。** 月別と日別は同じ列なので。"""
        from nippou.services import month_rollover

        self.save(self.full_row())
        from nippou.services import summary

        summary.refresh_shift(self.repo, DAY, "L-1", "1直")
        self.repo.mark_synced((DAY, "L-1", "1直", 1))
        result = month_rollover.run(self.repo, self.tmp / "月別",
                                    month=(2026, 8))
        detail = [p for p in result.exports[0].files if "明細" in p.name][0]

        self.wipe()
        found = csv_import.parse_file(detail)
        self.assertTrue(found.ok, found.problems)
        self.assertEqual(found.pages[0].details[0].lot, "H5422S0")


class ReadingTests(unittest.TestCase):
    """読み方 ── **間違ったまま入るより、入らないほうがまし。**"""

    def head(self) -> str:
        return csv_import.template_header()

    def test_見出しが足りなければ1行も入れない(self) -> None:
        found = csv_import.parse("ロット№,検入枚数\nA,9\n")
        self.assertFalse(found.ok)
        self.assertEqual(found.missing_columns, ["日付", "ライン", "直"])
        self.assertEqual(found.pages, [])

    def test_別の呼び名でも読む(self) -> None:
        """VBAの集計シートや、人が付けがちな短い名前。"""
        found = csv_import.parse(
            "報告日,設備,勤務,LotNo,検入,実績合計 枚数\n"
            f"{DAY},L-1,1,A1234,9,100\n")
        self.assertTrue(found.ok)
        detail = found.pages[0].details[0]
        self.assertEqual((detail.lot, detail.ken, detail.con),
                         ("A1234", "9", "100"))

    def test_むかしの頁という見出しも読む(self) -> None:
        """**自分が出した古いファイルを、自分で読めなくしない。**

        3.47.0 で見出しを「頁」から「ページ」に言い換えました。それより
        前に出したCSVは「頁」で書かれているので、取り込む側は両方を
        知っている必要があります ── 言い換えたその日に、共有フォルダに
        溜まっているぶんが全部読めなくなるのでは意味がありません。
        """
        for head in ("ページ", "頁", "頁番号", "ページ番号"):
            with self.subTest(head=head):
                found = csv_import.parse(
                    f"日付,ライン,直,{head},ロット№\n{DAY},L-1,1直,2,A\n")
                self.assertTrue(found.ok, found.problems)
                self.assertEqual(found.pages[0].header.page, 2)

    def test_全角や空白の違いを気にしない(self) -> None:
        found = csv_import.parse(
            "日付,ライン,直,ロット№,ＶＣ種別両面・片面\n"
            f"{DAY},L-1,１直,A,両面\n")
        self.assertTrue(found.ok, found.problems)
        self.assertEqual(found.pages[0].details[0].vc, "両面")

    def test_直の書き方をそろえる(self) -> None:
        for text, want in (("1", "1直"), ("1直", "1直"), ("一直", "1直"),
                           ("日勤", "日勤"), ("昼", "日勤")):
            with self.subTest(shift=text):
                found = csv_import.parse(
                    f"日付,ライン,直,ロット№\n{DAY},L-1,{text},A\n")
                self.assertEqual(found.pages[0].header.shift, want)

    def test_知らない列は黙って捨てる(self) -> None:
        """人が足したメモの列を「欠けている」とは言いません。"""
        found = csv_import.parse(
            f"日付,ライン,直,ロット№,担当のメモ\n{DAY},L-1,1直,A,あとで確認\n")
        self.assertTrue(found.ok)
        self.assertEqual(found.problems, [])

    def test_読めない行は落として理由を残す(self) -> None:
        """**1行のせいで全部止めません。** ただし黙って捨てもしません。"""
        found = csv_import.parse(
            "日付,ライン,直,ロット№\n"
            f",L-1,1直,A\n"
            f"{DAY},,1直,B\n"
            f"{DAY},L-1,9直,C\n"
            f"2026年13月99日,L-1,1直,D\n"
            f"{DAY},L-1,1直,E\n")
        self.assertEqual([p.row for p in found.problems], [2, 3, 4, 5])
        self.assertEqual([p.reason.split(":")[0] for p in found.problems],
                         ["日付がありません", "ラインがありません",
                          "直が読めません", "日付が読めません"])
        self.assertEqual(found.pages[0].details[0].lot, "E")

    def test_空の行は落としたと言わない(self) -> None:
        """12行のうち打ったのが1行なら、残りは紙の余白と同じ。"""
        found = csv_import.parse(
            "日付,ライン,直,行,ロット№\n"
            f"{DAY},L-1,1直,1,A\n"
            f"{DAY},L-1,1直,2,\n")
        self.assertEqual(len(found.pages[0].details), 1)
        self.assertEqual(found.problems, [])

    def test_空のファイルでも落ちない(self) -> None:
        found = csv_import.parse("")
        self.assertFalse(found.ok)
        self.assertEqual(found.problems[0].reason, "ファイルが空です")

    def test_cp932でも読む(self) -> None:
        """Excelの「CSV(コンマ区切り)」は cp932 で出ます。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sjis.csv"
            path.write_bytes(
                f"日付,ライン,直,ロット№\n{DAY},L-1,1直,ﾅﾒｶﾜ\n".encode("cp932"))
            found = csv_import.parse_file(path)
        self.assertTrue(found.ok)
        self.assertEqual(found.pages[0].details[0].lot, "ﾅﾒｶﾜ")

    def test_見本は書き出しの見出しと同じ(self) -> None:
        """**見本と出力が食い違うと、出したものが読めません。**"""
        self.assertEqual(csv_import.template_header(),
                         ",".join(csv_export.DETAIL_FIELDNAMES))


class PreviewTests(RepoTestCase):
    """下見 ── **押すまで書かない。**"""

    def csv(self, text: str) -> Path:
        path = self.tmp / "in.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def test_下見は1行も書かない(self) -> None:
        path = self.csv(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,A\n")
        found = import_service.preview(self.repo, path)
        self.assertTrue(found.ok)
        self.assertEqual(self.repo.list_keys(), [])   # 書いていない

    def test_置き換わるページを名指しで言う(self) -> None:
        """**数ではなく、どのページか。** 消えるほうの行数も添えます。"""
        self.save(row(lot="いまのA"), row(row_no=2, lot="いまのB"))
        path = self.csv(f"日付,ライン,直,ページ,ロット№\n"
                        f"{DAY},L-1,1直,1,新A\n"
                        f"{DAY},L-1,2直,1,新B\n")
        found = import_service.preview(self.repo, path)
        by_label = {t.label: t for t in found.targets}
        self.assertTrue(by_label[f"{DAY} L-1 1直 1ページ"].replaces)
        self.assertEqual(by_label[f"{DAY} L-1 1直 1ページ"].existing_rows, 2)
        self.assertFalse(by_label[f"{DAY} L-1 2直 1ページ"].replaces)
        self.assertIn("置き換えます", found.message)

    def test_置き換えが無ければ言わない(self) -> None:
        path = self.csv(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,A\n")
        self.assertNotIn("置き換え",
                         import_service.preview(self.repo, path).message)

    def test_見出しが足りなければ直し方を言う(self) -> None:
        path = self.csv("ロット№\nA\n")
        found = import_service.preview(self.repo, path)
        self.assertFalse(found.ok)
        self.assertIn("見出しが足りません", found.message)
        self.assertIn("日付", found.message)


class ApplyTests(RepoTestCase):
    """入れる ── **入れたら、全部の出口に載る。**"""

    def csv(self, text: str) -> Path:
        path = self.tmp / "in.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def test_入れたら記録に出る(self) -> None:
        path = self.csv(f"日付,ライン,直,ロット№,実績合計 枚数\n"
                        f"{DAY},L-1,1直,A,100\n")
        _preview, result = import_service.apply(self.repo, path)
        self.assertEqual((result.pages, result.rows), (1, 1))
        loaded = self.repo.load(DAY, "L-1", "1直", 1)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[1][0].lot, "A")

    def test_共有へは既定で送らない(self) -> None:
        """古いぶんは VBA の時代に既に入っている。**一斉に飛ばさない。**"""
        path = self.csv(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,A\n")
        import_service.apply(self.repo, path)
        self.assertEqual(self.repo.pending_sync_headers(), [])

    def test_送りたいときは送る(self) -> None:
        path = self.csv(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,A\n")
        import_service.apply(self.repo, path, mark_synced=False)
        self.assertEqual(len(self.repo.pending_sync_headers()), 1)

    def test_集計まで作る(self) -> None:
        """明細だけ入れると「記録には出るのにグラフには出ない」になる。"""
        path = self.csv(f"日付,ライン,直,ロット№,実績合計 枚数,実績合計 重量\n"
                        f"{DAY},L-1,1直,A,100,1200\n")
        _preview, result = import_service.apply(self.repo, path)
        self.assertEqual(result.summaries, 1)

        from nippou.services import summary

        rows = summary.day_rows(self.repo, DAY, "L-1")
        self.assertEqual(rows[0].sheet_count, 100)

    def test_読めなければ何もしない(self) -> None:
        path = self.csv("ロット№\nA\n")
        preview, result = import_service.apply(self.repo, path)
        self.assertFalse(preview.ok)
        self.assertEqual(result.pages, 0)
        self.assertEqual(self.repo.list_keys(), [])

    def test_置き換えた数を言う(self) -> None:
        self.save(row(lot="いまの"))
        path = self.csv(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,新しい\n")
        _preview, result = import_service.apply(self.repo, path)
        self.assertEqual(result.replaced, 1)
        self.assertIn("置き換え", result.message)
        self.assertEqual(self.repo.load(DAY, "L-1", "1直", 1)[1][0].lot, "新しい")


class ExportOnImportTests(RepoTestCase):
    """**取り込みは保存。** 保存(確定)と同じところまで進める。

    人が12行打って保存すると、日報 → 集計 → その日の集計CSV まで進みます。
    取り込みだけ集計で止めていたので、**紙とグラフには出るのに共有の
    フォルダにはその日が無い**、という食い違いが起きていました。
    """

    def csv(self, text: str) -> Path:
        path = self.tmp / "in.csv"
        path.write_text(text, encoding="utf-8-sig")
        return path

    def out(self) -> Path:
        return self.tmp / "out"

    def test_取り込んだ日のフォルダに3本出る(self) -> None:
        """**今日のフォルダではありません。** 入れた日報の作業日です。"""
        path = self.csv(f"日付,ライン,直,ロット№,実績合計 枚数,実績合計 重量\n"
                        f"{DAY},L-1,1直,A,100,1200\n")
        _preview, result = import_service.apply(self.repo, path,
                                                out_dir=self.out())
        folder = self.out() / "L-1" / "集計" / "2026.08" / "03"
        self.assertTrue(folder.is_dir(), f"{folder} が作られていません")
        names = sorted(p.name for p in folder.glob("*.csv"))
        self.assertEqual(len(names), import_service.CSV_PER_DAY, names)
        self.assertEqual((result.csv_days, result.csv_files),
                         (1, import_service.CSV_PER_DAY))
        # 読み方と計算内容は**年月のフォルダに1つ**(日ごとの書き出しと同じ)
        self.assertTrue((folder.parent / "CSVの読み方.txt").is_file())
        self.assertTrue((folder.parent / "計算内容.csv").is_file())
        self.assertFalse((folder / "CSVの読み方.txt").exists())

    def test_日ごとにフォルダが分かれる(self) -> None:
        path = self.csv(
            "日付,ライン,直,ロット№,実績合計 枚数\n"
            f"{DAY},L-1,1直,A,100\n"
            f"{DAY},L-1,2直,B,110\n"
            "2026年8月4日,L-1,1直,C,120\n")
        _preview, result = import_service.apply(self.repo, path,
                                                out_dir=self.out())
        # 3直ぶん入っても、フォルダは**日ごと**に1つ
        self.assertEqual(result.csv_days, 2)
        self.assertTrue((self.out() / "L-1" / "集計" / "2026.08" / "03").is_dir())
        self.assertTrue((self.out() / "L-1" / "集計" / "2026.08" / "04").is_dir())

    def test_出した中身は入れた値(self) -> None:
        path = self.csv(f"日付,ライン,直,ロット№,実績合計 枚数,実績合計 重量\n"
                        f"{DAY},L-1,1直,H5422S0,100,1200\n")
        import_service.apply(self.repo, path, out_dir=self.out())
        folder = self.out() / "L-1" / "集計" / "2026.08" / "03"
        detail = next(folder.glob("集計明細_*.csv"))
        text = detail.read_text(encoding="utf-8-sig")
        self.assertIn("H5422S0", text)

    def test_下見が何日ぶん書くかを言う(self) -> None:
        """**押す前に、書くものを全部言う。**"""
        path = self.csv("日付,ライン,直,ロット№\n"
                        f"{DAY},L-1,1直,A\n"
                        "2026年8月4日,L-1,1直,B\n")
        preview = import_service.preview(self.repo, path, out_dir=self.out())
        self.assertEqual(len(preview.days), 2)
        self.assertIn("2日ぶん", preview.message)
        self.assertIn(str(self.out()), preview.message)
        # **下見は1行も書かない。** CSVも出さない
        self.assertFalse(self.out().exists())

    def test_出せなくても取り込みは成功する(self) -> None:
        """日報が入らないほうが困ります。出せなかったことだけ言う。"""
        blocked = self.tmp / "ふさがっている"
        blocked.write_text("これはフォルダではありません", encoding="utf-8")
        path = self.csv(f"日付,ライン,直,ロット№\n{DAY},L-1,1直,A\n")
        _preview, result = import_service.apply(self.repo, path,
                                                out_dir=blocked)
        self.assertEqual(result.pages, 1)              # 日報は入っている
        self.assertIsNotNone(self.repo.load(DAY, "L-1", "1直", 1))
        self.assertEqual(result.csv_days, 0)
        self.assertEqual(len(result.csv_failed), 1)
        self.assertIn("日報は入っています", result.message)


# ======================================================================
# 画面から (Flask が要る)
# ======================================================================
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class WebTests(WebTestCase):
    """**管理者だけ。** 過去のデータを入れる操作なので。"""

    def csv(self, text: str = "") -> str:
        path = self.tmp / "in.csv"
        path.write_text(text or f"日付,ライン,直,ロット№\n{DAY},L-1,1直,A\n",
                        encoding="utf-8-sig")
        return str(path)

    def admin(self) -> None:
        from nippou import work_context

        work_context.get_context().admin = True

    def test_管理者でなければ断る(self) -> None:
        for url in ("/api/settings/import/preview",
                    "/api/settings/import/apply",
                    "/api/settings/import/template"):
            with self.subTest(url=url):
                res = self.post(url, {"path": self.csv()})
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.get_json()["error"]["code"], "not_admin")

    def test_下見は書かない(self) -> None:
        self.admin()
        body = self.post("/api/settings/import/preview",
                         {"path": self.csv()}).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["targets"][0]["label"], f"{DAY} L-1 1直 1ページ")
        self.assertEqual(self.repo().list_keys(), [])

    def test_押したら入る(self) -> None:
        self.admin()
        body = self.post("/api/settings/import/apply",
                         {"path": self.csv()}).get_json()
        self.assertEqual(body["pages"], 1)
        self.assertEqual(len(self.repo().list_keys()), 1)

    def test_ファイルが無ければ404(self) -> None:
        self.admin()
        res = self.post("/api/settings/import/preview",
                        {"path": str(self.tmp / "無い.csv")})
        self.assertEqual(res.status_code, 404)

    def test_道を書かなければ400(self) -> None:
        self.admin()
        res = self.post("/api/settings/import/preview", {})
        self.assertEqual(res.status_code, 400)

    def test_読めないCSVは422で断る(self) -> None:
        """**0件を「成功」と言わない。**"""
        self.admin()
        res = self.post("/api/settings/import/apply",
                        {"path": self.csv("ロット№\nA\n")})
        self.assertEqual(res.status_code, 422)
        self.assertIn("見出しが足りません", res.get_json()["message"])
        self.assertEqual(self.repo().list_keys(), [])

    def test_見本を出せる(self) -> None:
        self.admin()
        body = self.post("/api/settings/import/template", {}).get_json()
        path = Path(body["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8-sig").strip(),
                         csv_import.template_header())

    def test_画面に口がある(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("過去の日報を取り込む", html)
        self.assertIn("中身を見る(まだ入れない)", html)

    def test_落として渡せる(self) -> None:
        """**道を打たせない。** 共有フォルダの深いところだと現実的では
        ありませんし、10本あれば10回打つことになります。"""
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="import-drop"', html)
        self.assertIn('id="import-files"', html)
        self.assertIn("multiple", html)

    def test_共有をどうするかが読める文言になっている(self) -> None:
        """もとは「共有へは送らない(送信済みとして入れる)」でした。

        **何のことか読めません** ── 「送信済み」が何を指すのか、外すと
        何が起きるのかが、どちらも書いていませんでした。
        """
        html = self.get("/settings").get_data(as_text=True)
        self.assertNotIn("送信済みとして入れる", html)
        self.assertIn("そのままにする", html)
        self.assertIn("共有にも入れる", html)
        self.assertIn("この端末の中だけ", html)

    def test_入れたら紙にもグラフにも出る(self) -> None:
        """**入り口は1つ。** 下流は全部ついてきます。"""
        self.admin()
        self.post("/api/settings/import/apply", {"path": self.csv(
            f"日付,ライン,直,ロット№,実績合計 枚数,実績合計 重量,作業時間(分)\n"
            f"{DAY},L-1,1直,A,100,1200,84\n")})

        # グラフは期間の推移で見る(「本日」のタイルは今日のぶんなので、
        # 過去を入れても動きません ── そこが動いたら、それはそれで困ります)
        history = self.post("/api/graph/history",
                            {"start": "2026-08-01", "end": "2026-08-31"}
                            ).get_json()
        counts = {label: value for label, value in zip(
            history["labels"],
            next(s for s in history["series"] if s["key"] == "count")["values"])}
        self.assertEqual(counts[DAY], 100)

        paper = self.get(f"/report/nippou?report_date={DAY}&line=L-1&shift=1直")
        self.assertIn("A", paper.get_data(as_text=True))


if __name__ == "__main__":                       # pragma: no cover
    unittest.main()
