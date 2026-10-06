"""データアクセス層と表示テキスト組み立ての確認 (repository.py)。"""

import datetime as dt
import unittest

from calendar_app import config, db
from calendar_app.repository import (
    DayRecord,
    Repository,
    build_day_detail,
    build_day_text,
    build_day_tooltip,
)

D = dt.date(2026, 7, 25)


def make_record(**kwargs) -> DayRecord:
    """テスト用のレコードを作る。指定しない項目は空。"""
    base = dict(
        id=1,
        kubun=config.KUBUN_REST,
        naiyou="山田太郎",
        code="10",
        shift="",
        overtime="",
        early="",
        group="",
        line="",
    )
    base.update(kwargs)
    return DayRecord(**base)


class RepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.repo = Repository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _add_members(self) -> None:
        rows = [
            ("10", "杉山", "B", "杉山良宏", "すぎやま", "L-1"),
            ("11", "加藤", "A", "加藤眞一", "かとう", "L-1"),
            ("12", "岩田", "D", "岩田勇史", "いわた", "LVC"),
            ("50", "横井", "D", "横井友和", "よこい", "コイル"),
            ("60", "田中", "昼", "田中一郎", "たなか", "HVC"),
        ]
        self.conn.executemany(
            f'INSERT INTO "{config.TABLE_MEMBER}" '
            '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
            rows,
        )
        self.conn.commit()

    # -- 保存と参照 ----------------------------------------------------
    def test_save_and_read(self) -> None:
        new_id = self.repo.save_record(
            D, config.KUBUN_REST, "山田太郎", "10", "1", "佐藤", "鈴木", "B", "L-1"
        )
        self.assertGreater(new_id, 0)

        records = self.repo.get_day_records(D)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.naiyou, "山田太郎")
        self.assertEqual(record.shift, "1")
        self.assertEqual(record.overtime, "佐藤")
        self.assertEqual(record.group, "B")
        self.assertEqual(record.line, "L-1")
        self.assertEqual(record.date, D)
        self.assertTrue(record.is_rest)

    def test_month_records_grouped_by_date(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "A さん", "1")
        self.repo.save_record(D, config.KUBUN_REST, "B さん", "2")
        self.repo.save_record(D + dt.timedelta(days=1), config.KUBUN_REST, "C さん", "3")

        result = self.repo.get_month_records(D, D + dt.timedelta(days=5))
        self.assertEqual(len(result["2026/07/25"]), 2)
        self.assertEqual(len(result["2026/07/26"]), 1)

    def test_month_records_respects_range(self) -> None:
        self.repo.save_record(dt.date(2026, 6, 30), config.KUBUN_REST, "範囲外", "9")
        self.repo.save_record(D, config.KUBUN_REST, "範囲内", "1")
        result = self.repo.get_month_records(D, D + dt.timedelta(days=5))
        self.assertEqual(list(result), ["2026/07/25"])

    def test_exists_record(self) -> None:
        """同一日付+同一管理番号の二重登録を検出する。"""
        self.assertFalse(self.repo.exists_record(D, "10"))
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10")
        self.assertTrue(self.repo.exists_record(D, "10"))
        # 日付が違えば別扱い
        self.assertFalse(self.repo.exists_record(D + dt.timedelta(days=1), "10"))

    def test_sanitize_on_save(self) -> None:
        """タブや改行は取り除いて保存する (表示崩れ防止)。"""
        self.repo.save_record(D, config.KUBUN_OTHER, "1 行目\n2 行目\tタブ", "-")
        self.assertEqual(self.repo.get_day_records(D)[0].naiyou, "1 行目 2 行目 タブ")

    # -- 削除 ----------------------------------------------------------
    def test_delete_writes_history(self) -> None:
        record_id = self.repo.save_record(
            D, config.KUBUN_REST, "山田太郎", "10", group="B", line="L-1"
        )
        self.assertEqual(self.repo.delete_records_by_ids([record_id]), 1)
        self.assertEqual(self.repo.get_day_records(D), [])

        history = self.conn.execute(
            f'SELECT * FROM "{config.TABLE_DEL_HISTORY}"'
        ).fetchall()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["登録内容"], "山田太郎")
        self.assertEqual(history[0]["対象日付"], "2026/07/25")
        self.assertTrue(history[0]["削除実行者"])

    def test_delete_missing_id_is_ignored(self) -> None:
        self.assertEqual(self.repo.delete_records_by_ids([999]), 0)

    def test_delete_empty_list(self) -> None:
        self.assertEqual(self.repo.delete_records_by_ids([]), 0)

    # -- Access への反映待ち件数 ----------------------------------------
    def test_pending_count_after_save(self) -> None:
        self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10")
        self.assertEqual(self.repo.pending_sync_count(), 1)

    def test_pending_count_after_insert_then_delete_before_sync(self) -> None:
        """反映前に登録して削除した行は、休み管理側は相殺されて 0 になること。

        dbkit.outbox_sync は「今 SQLite テーブルに存在する行」しか見ないため、
        削除で行そのものが消えれば、それはもう「未送信」として数えられない。
        削除履歴への追記だけは残る (ローカルでの操作自体は記録するため)。
        """
        record_id = self.repo.save_record(D, config.KUBUN_REST, "山田太郎", "10")
        self.repo.delete_records_by_ids([record_id])
        # 休み管理の INSERT/DELETE は相殺され、削除履歴の INSERT だけが残る
        self.assertEqual(self.repo.pending_sync_count(), 1)

    def test_pending_count_after_delete_of_synced_row(self) -> None:
        """既に Access に存在する行 (access_id あり) を削除した場合は、
        削除履歴への追記に加えて休み管理側の削除転送も未送信として数える。
        """
        self.conn.execute(
            f'INSERT INTO "{config.TABLE_DATA}" '
            '("ID","access_id","日付","区分","登録内容","識別コード") VALUES (?,?,?,?,?,?)',
            (77, 77, "2026/07/25", config.KUBUN_REST, "既存の人", "50"),
        )
        self.conn.commit()
        self.repo.delete_records_by_ids([77])
        # 削除履歴 INSERT (1) + 休み管理からの削除転送予約 (1)
        self.assertEqual(self.repo.pending_sync_count(), 2)

    # -- 班員名簿 ------------------------------------------------------
    def test_get_workers_filters_by_line(self) -> None:
        self._add_members()
        names = [w.name for w in self.repo.get_workers("コイル")]
        self.assertEqual(names, ["横井友和"])

    def test_get_workers_supervisor_sees_all(self) -> None:
        self._add_members()
        self.assertEqual(len(self.repo.get_workers(config.LINE_SUPERVISOR)), 5)

    def test_get_workers_lvc_and_l1_are_paired(self) -> None:
        """LVC と L-1 は同一人物が兼務するため双方を対象にする。"""
        self._add_members()
        for line in ("LVC", "L-1"):
            names = sorted(w.name for w in self.repo.get_workers(line))
            self.assertEqual(names, ["加藤眞一", "岩田勇史", "杉山良宏"])

    def test_worker_day_shift_detection(self) -> None:
        self._add_members()
        workers = {w.name: w for w in self.repo.get_workers(config.LINE_SUPERVISOR)}
        self.assertFalse(workers["杉山良宏"].is_day_shift)  # B 班 = 3 交替
        self.assertTrue(workers["田中一郎"].is_day_shift)  # 昼 = 日勤

    def test_line_group_pairs(self) -> None:
        self._add_members()
        pairs = self.repo.get_line_group_pairs()
        self.assertIn(("L-1", "B"), pairs)
        self.assertIn(("コイル", "D"), pairs)
        self.assertEqual(len(pairs), len(set(pairs)))  # 重複なし

    def test_has_workers(self) -> None:
        self.assertFalse(self.repo.has_workers())
        self._add_members()
        self.assertTrue(self.repo.has_workers())


class BuildDayTextTest(unittest.TestCase):
    """カレンダーセルの表示絞り込み (VBA: BuildDayText) の確認。"""

    def test_rest_shows_line_and_group(self) -> None:
        records = [make_record(naiyou="杉山良宏", group="B", line="L-1")]
        self.assertEqual(build_day_text(records, "L-1"), "L-1:B班:杉山良宏 休")

    def test_supervisor_sees_everything(self) -> None:
        records = [
            make_record(id=1, naiyou="コイルの人", line="コイル", group="A"),
            make_record(id=2, naiyou="L1の人", line="L-1", group="B"),
        ]
        text = build_day_text(records, config.LINE_SUPERVISOR)
        self.assertIn("コイルの人", text)
        self.assertIn("L1の人", text)

    def test_coil_pc_sees_only_coil(self) -> None:
        records = [
            make_record(id=1, naiyou="コイルの人", line="コイル"),
            make_record(id=2, naiyou="L1の人", line="L-1"),
        ]
        text = build_day_text(records, "コイル")
        self.assertIn("コイルの人", text)
        self.assertNotIn("L1の人", text)

    def test_other_pc_hides_coil(self) -> None:
        records = [
            make_record(id=1, naiyou="コイルの人", line="コイル"),
            make_record(id=2, naiyou="L1の人", line="L-1"),
        ]
        text = build_day_text(records, "L-1")
        self.assertNotIn("コイルの人", text)
        self.assertIn("L1の人", text)

    def test_comments_only_for_own_line(self) -> None:
        records = [
            make_record(id=1, kubun=config.KUBUN_OTHER, naiyou="連絡", line="L-1", group="B"),
            make_record(id=2, kubun=config.KUBUN_OTHER, naiyou="別件", line="HVC", group="A"),
        ]
        text = build_day_text(records, "L-1")
        self.assertEqual(text, "L-1:B班 コメント有")

    def test_comments_are_collapsed_per_line_and_group(self) -> None:
        """同じライン+班のコメントは 1 行にまとめる。"""
        records = [
            make_record(id=i, kubun=config.KUBUN_OTHER, naiyou=f"連絡{i}", line="L-1", group="B")
            for i in range(3)
        ]
        self.assertEqual(build_day_text(records, "L-1"), "L-1:B班 コメント有")

    def test_empty(self) -> None:
        self.assertEqual(build_day_text([], "L-1"), "")


class BuildTooltipTest(unittest.TestCase):
    """残業/早出繋ぎのツールチップ (VBA: BuildDayTooltip)。"""

    def test_shift_neighbours(self) -> None:
        records = [
            make_record(naiyou="杉山良宏", shift="1", overtime="佐藤", early="鈴木",
                        group="B", line="L-1")
        ]
        text = build_day_tooltip(records)
        # 1 直の前は 3 直、次は 2 直
        self.assertIn("3直残業", text)
        self.assertIn("佐藤", text)
        self.assertIn("2直早出", text)
        self.assertIn("鈴木", text)

    def test_shift_wraparound(self) -> None:
        records = [make_record(shift="3", overtime="A", early="B")]
        text = build_day_tooltip(records)
        self.assertIn("2直残業", text)
        self.assertIn("1直早出", text)

    def test_unregistered_default(self) -> None:
        records = [make_record(shift="2")]
        text = build_day_tooltip(records)
        self.assertEqual(text.count(config.UNREGISTERED), 2)

    def test_multiple_entries_are_numbered(self) -> None:
        records = [
            make_record(id=1, naiyou="一人目", shift="1"),
            make_record(id=2, naiyou="二人目", shift="2"),
        ]
        text = build_day_tooltip(records)
        self.assertIn("[1]", text)
        self.assertIn("[2]", text)
        self.assertIn("-" * 20, text)

    def test_no_shift_means_no_tooltip(self) -> None:
        """日勤 (直なし) はツールチップを出さない。"""
        self.assertEqual(build_day_tooltip([make_record(shift="")]), "")

    def test_comments_are_excluded(self) -> None:
        records = [make_record(kubun=config.KUBUN_OTHER, naiyou="連絡", shift="1")]
        self.assertEqual(build_day_tooltip(records), "")


class BuildDetailTest(unittest.TestCase):
    """内容閲覧の詳細テキスト (VBA: BuildDayDetail)。"""

    def test_empty(self) -> None:
        self.assertEqual(build_day_detail([]), "登録はありません。")

    def test_rest_detail(self) -> None:
        records = [
            make_record(naiyou="杉山良宏", shift="1", overtime="佐藤", early="鈴木",
                        group="B", line="L-1")
        ]
        text = build_day_detail(records)
        self.assertIn("[休み] L-1 B班 杉山良宏 (1直)", text)
        self.assertIn("3直残業: 佐藤", text)
        self.assertIn("2直早出: 鈴木", text)

    def test_comment_detail(self) -> None:
        records = [make_record(kubun=config.KUBUN_OTHER, naiyou="朝礼あり", group="D")]
        text = build_day_detail(records)
        self.assertIn("D班", text)
        self.assertIn("朝礼あり", text)

    def test_separator_between_entries(self) -> None:
        records = [make_record(id=1, naiyou="一人目"), make_record(id=2, naiyou="二人目")]
        self.assertIn("-" * 30, build_day_detail(records))


if __name__ == "__main__":
    unittest.main()
