import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.reporting import csv_export
from nippou.db.connection import connect
from nippou.db.models import DetailRecord, HeaderRecord
from nippou.db.repository import NippouRepository
from nippou.reporting.csv_export import (
    DETAIL_FIELDNAMES,
    FIELDNAMES,
    KIND_AGGREGATE,
    KIND_PRINT,
    STOP_FIELDNAMES,
    build_daily_aggregate_rows,
    build_detail_rows,
    build_stop_rows,
    daily_output_paths,
    dated_dir,
    default_detail_output_path,
    default_output_path,
    default_stop_output_path,
    line_dir,
    write_daily_aggregate_csv,
    write_daily_detail_csv,
    write_daily_stop_csv,
)


def make_header(**overrides) -> HeaderRecord:
    base = dict(report_date="2026年8月3日", line="L-1", shift="1直", page=1, count="10", weight_kg="1000")
    base.update(overrides)
    return HeaderRecord(**base)


class CsvExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmpdir.name) / "test.sqlite3")
        self.repo = NippouRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmpdir.cleanup()

    def test_build_daily_aggregate_rows_one_row_per_shift(self) -> None:
        for shift, count, weight in (("1直", "10", "1000"), ("2直", "8", "800")):
            header = make_header(shift=shift, count=count, weight_kg=weight)
            self.repo.save(header, [DetailRecord(
                report_date=header.report_date, line=header.line, shift=shift,
                page=1, row_no=1, con=count, wei=weight, tim="30")])
        rows = build_daily_aggregate_rows(self.repo, "2026年8月3日", "L-1")
        self.assertEqual([r.shift for r in rows], ["1直", "2直"])
        self.assertEqual(rows[0].sheet_count, 10)
        self.assertEqual(rows[1].sheet_count, 8)

    def test_build_daily_aggregate_rows_excludes_other_lines_and_dates(self) -> None:
        self.repo.save(make_header(line="L-1"), [])
        self.repo.save(make_header(line="L2"), [])
        self.repo.save(make_header(report_date="2026年8月4日"), [])
        rows = build_daily_aggregate_rows(self.repo, "2026年8月3日", "L-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].line, "L-1")

    def test_write_daily_aggregate_csv_creates_readable_file(self) -> None:
        header = make_header(shift="1直", count="12", weight_kg="1234")
        self.repo.save(header, [DetailRecord(
            report_date=header.report_date, line=header.line, shift="1直", page=1, row_no=1,
            con="12", wei="1234", s="1", th="20", tim="45")])
        out_path = Path(self._tmpdir.name) / "out" / "agg.csv"

        row_count = write_daily_aggregate_csv(self.repo, "2026年8月3日", "L-1", out_path)

        self.assertEqual(row_count, 1)
        self.assertTrue(out_path.exists())
        with open(out_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, FIELDNAMES)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ライン"], "L-1")
        self.assertEqual(rows[0]["直"], "1直")
        self.assertEqual(rows[0]["合計枚数"], "12")
        self.assertEqual(rows[0]["管理ロス設備停止(分)"], "20")
        # 3分類の合計も出す(Excelで足し直させない)
        self.assertEqual(rows[0]["停止合計(分)"], "20")
        self.assertEqual(rows[0]["稼働時間合計(分)"], "1420")   # 1440 − 20
        self.assertEqual(rows[0]["操業時間(分)"], "1420")       # 1440 − 管理ロス

    def test_write_daily_aggregate_csv_creates_parent_dir(self) -> None:
        out_path = Path(self._tmpdir.name) / "nested" / "dir" / "agg.csv"
        write_daily_aggregate_csv(self.repo, "2026年8月3日", "L-1", out_path)
        self.assertTrue(out_path.parent.is_dir())

    def test_write_daily_aggregate_csv_no_data_still_writes_header_only(self) -> None:
        out_path = Path(self._tmpdir.name) / "empty.csv"
        row_count = write_daily_aggregate_csv(self.repo, "2026年8月3日", "L-1", out_path)
        self.assertEqual(row_count, 0)
        with open(out_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, FIELDNAMES)
            self.assertEqual(list(reader), [])


class DetailCsvTests(unittest.TestCase):
    """明細のほう(VBA `Agg_OutPut` が集計シートの表に並べていたもの)。

    **紙に載らない欄がここにしか出ない。** 用途コード / 用途名 / 納入先 /
    包装仕様書No / コイル縦割 / コイル横縦割 は印刷範囲の外(AN〜AS)で、
    VBA も集計シートにだけ書いていた。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.conn = connect(self.tmp / "test.sqlite3")
        self.repo = NippouRepository(self.conn)
        self.addCleanup(self._tmpdir.cleanup)
        self.addCleanup(self.conn.close)

    def _save(self, **detail) -> None:
        header = make_header(worker="山田 田中", day_shift="無", reason="設備移動")
        base = dict(report_date=header.report_date, line=header.line,
                    shift=header.shift, page=1, row_no=1, lot="H5422S0")
        base.update(detail)
        self.repo.save(header, [DetailRecord(**base)])

    def _rows(self) -> list[dict[str, str]]:
        return build_detail_rows(self.repo, "2026年8月3日", "L-1")

    def test_納入先などの印刷範囲外も出る(self) -> None:
        self._save(others1="H283", others2="JISNﾌﾗﾂﾄANF", others3="ﾅﾒｶﾜｱﾙﾐ(ｶ",
                   others4="1P1186", others5="縦", others6="横")
        row = self._rows()[0]
        self.assertEqual(row["用途コード"], "H283")
        self.assertEqual(row["用途名"], "JISNﾌﾗﾂﾄANF")
        self.assertEqual(row["納入先"], "ﾅﾒｶﾜｱﾙﾐ(ｶ")
        self.assertEqual(row["包装仕様書No"], "1P1186")
        self.assertEqual(row["コイル縦割"], "縦")
        self.assertEqual(row["コイル横縦割"], "横")

    def test_見出しは紙と同じ名前(self) -> None:
        """`others3` では誰にも読めない。名前は `layout.py` が1か所で持つ。"""
        for label in ("用途コード", "用途名", "納入先", "包装仕様書No",
                      "コイル縦割", "コイル横縦割"):
            self.assertIn(label, DETAIL_FIELDNAMES, label)

    def test_停止の記号は上段付きで見分ける(self) -> None:
        """下段だけだと「記号」が3つ並んで見分けが付かない。"""
        for label in ("作業停止① 記号", "作業停止② 記号", "作業停止③ 記号"):
            self.assertIn(label, DETAIL_FIELDNAMES, label)

    def test_直に1つのものも行ごとに入る(self) -> None:
        """並べ替えても、どの直の行かが分かるように。"""
        self._save(ken="9")
        row = self._rows()[0]
        self.assertEqual(row["作業者"], "山田 田中")
        self.assertEqual(row["昼稼働"], "無")
        self.assertEqual(row["作業コメント"], "設備移動")

    def test_係数処理ﾛｯﾄ数も出る(self) -> None:
        self._save(keisu="3")
        self.assertEqual(self._rows()[0]["係数処理ﾛｯﾄ数"], "3")

    def test_引当番号も残す(self) -> None:
        """共有へは送らない控えだが、月別の書き出しでは最後の写しになる。"""
        self._save(hiki_no="60717001")
        self.assertEqual(self._rows()[0]["引当番号"], "60717001")

    def test_空の行は落とす(self) -> None:
        """12行のうち打ったのが1行なら、残りは紙の余白と同じ。"""
        header = make_header()
        rows = [DetailRecord(report_date=header.report_date, line=header.line,
                             shift=header.shift, page=1, row_no=n,
                             lot="A" if n == 1 else "")
                for n in range(1, 13)]
        self.repo.save(header, rows)
        self.assertEqual(len(self._rows()), 1)

    def test_書き出したファイルが読み直せる(self) -> None:
        self._save(others3="ﾅﾒｶﾜｱﾙﾐ(ｶ", con="10", wei="1000")
        out = self.tmp / "out" / "detail.csv"
        count = write_daily_detail_csv(self.repo, "2026年8月3日", "L-1", out)
        self.assertEqual(count, 1)
        with open(out, encoding="utf-8-sig", newline="") as f:
            read = list(csv.DictReader(f))
        self.assertEqual(read[0]["納入先"], "ﾅﾒｶﾜｱﾙﾐ(ｶ")
        self.assertEqual(read[0]["実績合計 重量"], "1000")

    def test_保存が無くても見出しだけは書く(self) -> None:
        out = self.tmp / "out" / "empty.csv"
        self.assertEqual(
            write_daily_detail_csv(self.repo, "2026年8月3日", "L-1", out), 0)
        with open(out, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, DETAIL_FIELDNAMES)


class SourceTests(unittest.TestCase):
    """出どころ ── **入力 → 集計 → CSV の一直線。**

    以前はCSVだけが打った明細を読んで計算し直していました。数は合いますが、
    グラフ(残してある集計を読む)とCSVで**別の道**を通ることになり、
    「入力を計算したものが集計、それをCSVで吐く」と一言で言えません
    でした。いまは4本とも集計から作ります。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.conn = connect(self.tmp / "test.sqlite3")
        self.repo = NippouRepository(self.conn)
        self.addCleanup(self._tmpdir.cleanup)
        self.addCleanup(self.conn.close)

    def save(self, **detail) -> None:
        header = make_header(worker="山田")
        base = dict(report_date=header.report_date, line=header.line,
                    shift=header.shift, page=1, row_no=1, lot="H5422S0")
        base.update(detail)
        self.repo.save(header, [DetailRecord(**base)])

    def test_集計をまだ作っていなくても出る(self) -> None:
        """**CSVだけ空になる、が起きない。**

        集計は保存のたびに作っていますが、この仕組みが入る前に保存された
        ぶんや、表を直に書き換えたぶんには集計がありません。読む前に
        `summary.fill_missing` が作るので、CSVは必ず埋まります。
        """
        self.save(con="12", wei="1234", tim="45")
        # 集計テーブルを空にしてから読む(この仕組みが入る前の状態)
        self.conn.execute("DELETE FROM packing_report")
        self.conn.commit()

        rows = build_daily_aggregate_rows(self.repo, "2026年8月3日", "L-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].sheet_count, 12)
        self.assertEqual(len(build_detail_rows(self.repo, "2026年8月3日", "L-1")), 1)

    def test_打った数とCSVの数が一致する(self) -> None:
        """道が変わっても**数は変わらない**こと。"""
        from nippou.logic.aggregation import aggregate_day

        self.save(con="12", wei="1234", tim="45", s="1", th="20")
        typed = aggregate_day(
            self.repo.list_headers_for_date("2026年8月3日", line="L-1"))[0]
        from_summary = build_daily_aggregate_rows(self.repo, "2026年8月3日", "L-1")[0]

        self.assertEqual(from_summary.sheet_count, typed.sheet_count)
        self.assertEqual(from_summary.weight_kg, typed.weight_kg)
        self.assertEqual(from_summary.work_minutes, typed.work_minutes)
        self.assertEqual(from_summary.total_stop_minutes,
                         typed.total_stop_minutes)
        self.assertEqual(from_summary.operating_rate_pct,
                         typed.operating_rate_pct)

    def test_グラフと同じものを読んでいる(self) -> None:
        """**画面の数字とCSVの数字が食い違わない。** 出どころが同じなので。"""
        from nippou.services import summary

        self.save(con="12", wei="1234", tim="45")
        screen = summary.day_rows(self.repo, "2026年8月3日", "L-1")
        csv_rows = build_daily_aggregate_rows(self.repo, "2026年8月3日", "L-1")
        self.assertEqual([(r.shift, r.sheet_count, r.weight_kg) for r in screen],
                         [(r.shift, r.sheet_count, r.weight_kg) for r in csv_rows])

    # -- 見え方(集計は「値」なので、打った文字そのものではない) --------
    def test_時分は2桁に詰め直す(self) -> None:
        """紙は "08" と書いてある。集計には 8 が入っているので戻す。"""
        self.save(kz="08", kh="00", sz="10", sh="05")
        row = build_detail_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(row["梱包作業時間 開始 時"], "08")
        self.assertEqual(row["梱包作業時間 開始 分"], "00")   # 0分は0分
        self.assertEqual(row["梱包作業時間 終了 時"], "10")
        self.assertEqual(row["梱包作業時間 終了 分"], "05")

    def test_打っていない時分は空欄のまま(self) -> None:
        """**0時00分と、打っていないのは違う。**"""
        self.save(con="12")
        row = build_detail_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(row["梱包作業時間 開始 時"], "")
        self.assertEqual(row["梱包作業時間 開始 分"], "")

    def test_整数に小数点を出さない(self) -> None:
        """集計は 1000.0 で持っている。打った人は「1000」と書いた。"""
        self.save(con="10", wei="1000", ken="9", uni="2.5")
        row = build_detail_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(row["実績合計 枚数"], "10")
        self.assertEqual(row["実績合計 重量"], "1000")
        self.assertEqual(row["検入枚数"], "9")
        self.assertEqual(row["単重"], "2.5")          # 割り切れないものは残す

    def test_打っていない数は空欄のまま(self) -> None:
        """集計では0が入っている。**12行×30列が0で埋まると読めない。**"""
        self.save(con="10")
        row = build_detail_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(row["検入枚数"], "")
        self.assertEqual(row["作業人数"], "")
        self.assertEqual(row["実績合計 重量"], "")

    def test_停止は縦から横へ戻す(self) -> None:
        """集計は1停止1行。紙は①②③が横に並んでいる。"""
        self.save(s="1", th="30", ss="イ", ths="20", sth="A", tht="10")
        row = build_detail_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(row["作業停止① 記号"], "1")
        self.assertEqual(row["作業停止① 時間(分)"], "30")
        self.assertEqual(row["作業停止② 記号"], "イ")
        self.assertEqual(row["作業停止③ 記号"], "A")

    def test_空いた番号は詰めない(self) -> None:
        """②だけ書いた行を①へ詰めると、紙と見比べたときに合わない。"""
        self.save(ss="イ", ths="20")
        row = build_detail_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(row["作業停止① 記号"], "")
        self.assertEqual(row["作業停止② 記号"], "イ")
        self.assertEqual(row["作業停止② 時間(分)"], "20")

    def test_並びは日付から行まで(self) -> None:
        """紙をめくる順。日付 → 直 → ページ → 行。"""
        header = make_header(shift="2直")
        self.repo.save(header, [DetailRecord(
            report_date=header.report_date, line="L-1", shift="2直",
            page=1, row_no=1, lot="B")])
        self.save(lot="A")
        rows = build_detail_rows(self.repo, "2026年8月3日", "L-1")
        self.assertEqual([r["直"] for r in rows], ["1直", "2直"])


class StopCsvTests(unittest.TestCase):
    """停止の内訳 ── **分類の合計で止まらない。**

    集計CSVには3分類の合計しか出ていませんでした。「段取り・突発停止が
    120分」までは分かっても、それが機械の突発なのかﾌｫｰｸ待ちなのかは
    出ません ── **手が打てるのは項目まで降りたときだけ**です。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = NippouRepository(connect(Path(self._tmpdir.name) / "t.sqlite3"))
        self.addCleanup(self.repo.conn.close)   # 開いたままでは、Windows で一時フォルダを消せない
        # **参照先を仮の場所へ寄せる。**
        #
        # 「内訳名が読めなくても記号で出す」は、内訳マスタが**読めない**
        # ことを前提にしています。既定は `~/NippouGwRef` なので、その端末に
        # 本物のマスタがあるかどうかで結果が変わっていました ── 動く端末と
        # 動かない端末が出るテストは、通っても何も保証しません。
        before = os.environ.get("NIPPOU_GW_REF_DIR")
        os.environ["NIPPOU_GW_REF_DIR"] = str(Path(self._tmpdir.name) / "ref")
        self.addCleanup(
            lambda: os.environ.pop("NIPPOU_GW_REF_DIR", None) if before is None
            else os.environ.__setitem__("NIPPOU_GW_REF_DIR", before))

    def save(self, shift: str, **stops) -> None:
        """1行だけ打ったページを1つ。`stops` は 記号→分 の組を最大3つ。"""
        header = make_header(shift=shift)
        pairs = list(stops.items())
        fields = {}
        for (code, minutes), (c, m) in zip(pairs, (("s", "th"), ("ss", "ths"),
                                                   ("sth", "tht"))):
            fields[c] = code
            fields[m] = str(minutes)
        self.repo.save(header, [DetailRecord(
            report_date=header.report_date, line="L-1", shift=shift,
            page=1, row_no=1, con="10", wei="1000", tim="60", **fields)])

    def rows(self):
        """**まとめの行だけ。** 1件ずつの行は下の `ones()` で見ます。"""
        return [r for r in self.all_rows()
                if r["区分"] == csv_export.STOP_KIND_TOTAL]

    def all_rows(self):
        return build_stop_rows(self.repo, "2026年8月3日", "L-1")

    def ones(self):
        """その記号が**どの行で**効いたか、の行だけ。"""
        return [r for r in self.all_rows()
                if r["区分"] == csv_export.STOP_KIND_ONE]

    def test_記号ごとに1行(self) -> None:
        self.save("1直", **{"1": 30, "イ": 20, "A": 10})
        rows = self.rows()
        self.assertEqual([r["記号"] for r in rows], ["1", "イ", "A"])
        self.assertEqual([r["停止時間(分)"] for r in rows], ["30", "20", "10"])

    def test_分類は現場の呼び名(self) -> None:
        """VBA の `StopM` / `StopH` に添えてあった呼び名で出す。"""
        self.save("1直", **{"1": 30, "イ": 20, "A": 10})
        self.assertEqual([r["分類"] for r in self.rows()],
                         ["管理ロス設備停止", "段取り・突発停止",
                          "ハンドリング停止"])

    def test_直を混ぜない(self) -> None:
        """**日でまとめると「3直だけ長い」が見えなくなる。**"""
        self.save("1直", **{"1": 30})
        self.save("3直", **{"1": 90})
        rows = self.rows()
        self.assertEqual([r["直"] for r in rows], ["1直", "3直"])
        self.assertEqual([r["停止時間(分)"] for r in rows], ["30", "90"])

    def test_直の並びは紙の順(self) -> None:
        """五十音でも保存順でもなく 1直→2直→3直→日勤。"""
        for shift in ("3直", "1直", "日勤", "2直"):
            self.save(shift, **{"1": 10})
        self.assertEqual([r["直"] for r in self.rows()],
                         ["1直", "2直", "3直", "日勤"])

    def test_分類の中は長い順(self) -> None:
        """分類をまたいで長い順にすると、色分けした表がまだらになる。"""
        self.save("1直", **{"イ": 5, "1": 30, "ロ": 60})
        rows = self.rows()
        self.assertEqual([r["記号"] for r in rows], ["1", "ロ", "イ"])

    def test_割合はその直の中で(self) -> None:
        """1日の中にすると、直が1つの日と3つの日で読み方が変わる。"""
        self.save("1直", **{"1": 30, "イ": 10})
        self.save("2直", **{"1": 100})
        pct = {(r["直"], r["記号"]): r["その直の停止に占める割合(%)"]
               for r in self.rows()}
        self.assertEqual(pct[("1直", "1")], "75.0")     # 30/40
        self.assertEqual(pct[("1直", "イ")], "25.0")
        self.assertEqual(pct[("2直", "1")], "100.0")    # 1直ぶんは混ざらない

    def test_全部0でも落ちない(self) -> None:
        self.save("1直", **{"1": 0})
        self.assertEqual([r["その直の停止に占める割合(%)"] for r in self.rows()],
                         [""])

    def test_停止が無ければ0行(self) -> None:
        self.save("1直")
        self.assertEqual(self.all_rows(), [])

    def test_内訳名が読めなくても記号で出す(self) -> None:
        """**内訳名が無いことより、停止が出ないことのほうが困る。**

        内訳名は保存したときにマスタから拾って集計へ書いてあります。
        マスタが読めない端末では空のままですが、記号と時間は出ます。
        """
        self.save("1直", **{"1": 30})
        self.assertEqual(self.rows()[0]["内訳"], "")

    def test_ファイルに書ける(self) -> None:
        self.save("1直", **{"1": 30, "イ": 20})
        out = Path(self._tmpdir.name) / "out" / "stop.csv"
        count = write_daily_stop_csv(self.repo, "2026年8月3日", "L-1", out)
        # まとめ2行 + その中身が1件ずつで2行
        self.assertEqual(count, 4)
        with open(out, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, STOP_FIELDNAMES)
            self.assertEqual(len(list(reader)), 4)

    def test_停止が無くても見出しは出す(self) -> None:
        out = Path(self._tmpdir.name) / "stop.csv"
        self.assertEqual(
            write_daily_stop_csv(self.repo, "2026年8月3日", "L-1", out), 0)
        with open(out, encoding="utf-8-sig", newline="") as f:
            self.assertEqual(csv.DictReader(f).fieldnames, STOP_FIELDNAMES)

    def test_合計は集計CSVと一致する(self) -> None:
        """**2本のCSVで違う数が出ない。** 出どころは同じ明細。"""
        self.save("1直", **{"1": 30, "イ": 20, "A": 10})
        stop_total = sum(float(r["停止時間(分)"]) for r in self.rows())
        agg = build_daily_aggregate_rows(self.repo, "2026年8月3日", "L-1")[0]
        self.assertEqual(stop_total, agg.total_stop_minutes)

    def test_どの行の停止かが出る(self) -> None:
        """**利用者の言葉:「停止内容がどの行に効いてるのかわからない」**

        まとめだけだと「ﾌｫｰｸ待ち 45分」までしか出ず、そこから先は紙を
        開いて12行を目で追うことになっていました。
        """
        header = make_header(shift="1直", page=1)
        self.repo.save(header, [
            DetailRecord(report_date=header.report_date, line="L-1",
                         shift="1直", page=1, row_no=3, lot="H5422S0",
                         s="イ", th="20"),
            DetailRecord(report_date=header.report_date, line="L-1",
                         shift="1直", page=1, row_no=7, lot="N7131T0",
                         ss="イ", ths="15"),
        ])
        ones = self.ones()
        self.assertEqual([(r["ページ"], r["行"], r["LOTNO"], r["停止の位置"],
                           r["停止時間(分)"]) for r in ones],
                         [("1", "3", "H5422S0", "作業停止①", "20"),
                          ("1", "7", "N7131T0", "作業停止②", "15")])

    def test_1件ずつはまとめのすぐ下に並ぶ(self) -> None:
        """離れていると、どのまとめの中身なのかが読めません。"""
        self.save("1直", **{"1": 30, "イ": 20})
        kinds = [r["区分"] for r in self.all_rows()]
        self.assertEqual(kinds, [csv_export.STOP_KIND_TOTAL,
                                 csv_export.STOP_KIND_ONE,
                                 csv_export.STOP_KIND_TOTAL,
                                 csv_export.STOP_KIND_ONE])
        # 記号も連れ立っている
        self.assertEqual([r["記号"] for r in self.all_rows()],
                         ["1", "1", "イ", "イ"])

    def test_1件ずつは紙をめくる順(self) -> None:
        """ページ → 行 → ①②③。読む人はページを開いて上から下へ目を落とす。"""
        # **ページは header が決めます。** ページごとに保存する
        first = make_header(shift="1直", page=1)
        self.repo.save(first, [
            DetailRecord(report_date=first.report_date, line="L-1",
                         shift="1直", page=1, row_no=9, lot="B", s="1", th="5"),
            DetailRecord(report_date=first.report_date, line="L-1",
                         shift="1直", page=1, row_no=2, lot="A",
                         s="1", th="5", ss="1", ths="5"),
        ])
        second = make_header(shift="1直", page=2)
        self.repo.save(second, [
            DetailRecord(report_date=second.report_date, line="L-1",
                         shift="1直", page=2, row_no=1, lot="C", s="1", th="5"),
        ])
        self.assertEqual([(r["ページ"], r["行"], r["停止の位置"]) for r in self.ones()],
                         [("1", "2", "作業停止①"), ("1", "2", "作業停止②"),
                          ("1", "9", "作業停止①"), ("2", "1", "作業停止①")])

    def test_まとめの行にページや行は入れない(self) -> None:
        """まとめは直ぶんなので、1つの行を指せません。**空にします。**"""
        self.save("1直", **{"1": 30})
        total = self.rows()[0]
        self.assertEqual((total["ページ"], total["行"], total["LOTNO"],
                          total["停止の位置"]), ("", "", "", ""))

    def test_1件ずつには割合を入れない(self) -> None:
        """まとめの割合と並ぶと、どちらを読むのか分からなくなります。"""
        self.save("1直", **{"1": 30, "イ": 10})
        self.assertEqual({r["その直の停止に占める割合(%)"] for r in self.ones()},
                         {""})

    def test_1件ずつを足すとまとめになる(self) -> None:
        """**どちらを読んでも同じ数。** 足し忘れ・二重計上が無いこと。"""
        self.save("1直", **{"1": 30, "イ": 20, "A": 10})
        self.save("2直", **{"1": 15})
        ones = sum(float(r["停止時間(分)"]) for r in self.ones())
        totals = sum(float(r["停止時間(分)"]) for r in self.rows())
        self.assertEqual(ones, totals)


class DefaultOutputPathTests(unittest.TestCase):
    def test_明細は別のファイル名(self) -> None:
        path = default_detail_output_path(
            Path("/tmp/reports"), "2026年8月3日", "L-1")
        self.assertEqual(
            path,
            Path("/tmp/reports/L-1/集計/2026.08/03/集計明細_2026-08-03_L-1.csv"))

    def test_uses_iso_date_and_line(self) -> None:
        path = default_output_path(Path("/tmp/reports"), "2026年8月3日", "L-1")
        self.assertEqual(
            path,
            Path("/tmp/reports/L-1/集計/2026.08/03/集計_2026-08-03_L-1.csv"))

    def test_ラインと種類で分ける(self) -> None:
        """**出力先には共有のフォルダを指せます。**

        そこへ全ラインが同じ構成で書くと、`集計_2026-08-03_機側.csv` と
        `集計_2026-08-03_L-1.csv` が同じフォルダに混ざり、自分のラインの
        ぶんを探すのに名前の後ろを1件ずつ読むことになります。
        """
        self.assertEqual(line_dir(Path("/tmp/r"), "L-1", KIND_AGGREGATE),
                         Path("/tmp/r/L-1/集計"))
        self.assertEqual(line_dir(Path("/tmp/r"), "L-1", KIND_PRINT),
                         Path("/tmp/r/L-1/印刷"))

    def test_年月日のフォルダに出す(self) -> None:
        """**指定のパスの下に ライン/種類/年月/日。**

        1フォルダに出し続けると1ラインでも1か月で120本を超えます。
        月のフォルダを開けば操業した日だけが並ぶようにします。
        """
        self.assertEqual(
            dated_dir(Path("/tmp/reports"), "2026年8月3日", "L-1"),
            Path("/tmp/reports/L-1/集計/2026.08/03"))

    def test_ラインを言わなければ根のまま(self) -> None:
        """ラインが決まっていない呼び出しでも、出ないよりは出す。"""
        self.assertEqual(dated_dir(Path("/tmp/reports"), "2026年8月3日"),
                         Path("/tmp/reports/2026.08/03"))

    def test_月日は2桁で揃える(self) -> None:
        """名前順が日付順になるように。**9月と10月を入れ替えない。**"""
        nine = dated_dir(Path("/tmp/r"), "2026年9月1日", "L-1")
        ten = dated_dir(Path("/tmp/r"), "2026年10月1日", "L-1")
        self.assertEqual(nine.parent.name, "2026.09")
        self.assertEqual(ten.parent.name, "2026.10")
        self.assertLess(nine.parent.name, ten.parent.name)
        self.assertEqual(nine.name, "01")

    def test_3本が同じ日のフォルダに揃う(self) -> None:
        """開くのはファイルではなく**フォルダ**なので、揃っていること。

        計算内容は日のフォルダには出しません(年月のフォルダに1つ。v4.2.0)。"""
        paths = daily_output_paths(Path("/tmp/reports"), "2026年8月3日", "L-1")
        self.assertEqual({p.parent for p in paths.values()},
                         {Path("/tmp/reports/L-1/集計/2026.08/03")})
        self.assertEqual(set(paths), {"aggregate", "detail", "stop"})

    def test_falls_back_to_raw_string_for_unparsable_date(self) -> None:
        """**日が読めなくても出します。** 置き場所が決まらないことより、
        出ないことのほうが困るので、日のフォルダを作らずラインの下へ。"""
        path = default_output_path(Path("/tmp/reports"), "not-a-date", "L-1")
        self.assertEqual(path,
                         Path("/tmp/reports/L-1/集計/集計_not-a-date_L-1.csv"))

    def test_sanitizes_unsafe_characters_in_line_name(self) -> None:
        path = default_output_path(Path("/tmp/reports"), "2026年8月3日", "L/1:*")
        self.assertEqual(path.name, "集計_2026-08-03_L1.csv")
        # **フォルダの名前も同じ直し方で。** 片方だけ直すと
        # `L/1` が区切りとして効いてフォルダが1段増えます
        self.assertEqual(path.parent,
                         Path("/tmp/reports/L1/集計/2026.08/03"))


if __name__ == "__main__":
    unittest.main()
