"""フォルダ構成からの点検表一覧（VBA版 GetWorkbooksFromFolder / CreateCheckBoxes と同じ規則）。"""
import os
import unittest

from tests.helpers import make_tree, temp_dir

from app.services.inspection_service import display_name, make_item_id, scan_folder


class DisplayNameTest(unittest.TestCase):
    def test_prefix_is_removed(self):
        self.assertEqual(display_name("梱包", "日常点検", "梱包_日常点検_台車点検", True), "台車点検")

    def test_prefix_is_case_insensitive(self):
        self.assertEqual(display_name("NS1", "Line", "ns1_line_Check", True), "Check")

    def test_name_is_trimmed_like_vba(self):
        self.assertEqual(display_name("A", "B", "  A_B_x  ", True), "x")

    def test_without_prefix_is_hidden_when_required(self):
        self.assertIsNone(display_name("A", "B", "旧様式_検査記録", True))

    def test_without_prefix_is_shown_when_not_required(self):
        self.assertEqual(display_name("A", "B", "旧様式", False), "旧様式")


class ScanFolderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.root = self._tmp.name
        make_tree(self.root, [
            "梱包/日常点検/梱包_日常点検_台車点検.xlsx",
            "梱包/日常点検/梱包_日常点検_コンベア.XLSM",
            "梱包/日常点検/梱包_日常点検_古い様式.xls",
            "梱包/日常点検/~$梱包_日常点検_台車点検.xlsx",   # Excel のロックファイル
            "梱包/日常点検/梱包_日常点検_メモ.txt",           # 対象外の拡張子
            "梱包/日常点検/旧_検査記録.xlsx",                 # 命名規則外
            "梱包/月次点検/梱包_月次点検_消火器.xlsx",
            "梱包/空のサブ/readme.txt",                       # Excel なし → 表示しない
            "梱包/カテゴリ直下.xlsx",                          # サブカテゴリーに入っていない → 対象外
            "b_category/sub/b_category_sub_one.xlsx",
            "A_category/sub/A_category_sub_two.xlsx",
        ])

    def tearDown(self):
        self._tmp.cleanup()

    def test_structure_and_counts(self):
        inv = scan_folder(self.root, ["xlsm", "xlsx", "xls"], True)
        self.assertTrue(inv.root_exists)
        # vbTextCompare 相当（大文字小文字を区別しない）の昇順
        self.assertEqual([c.name for c in inv.categories], ["A_category", "b_category", "梱包"])
        konpo = inv.categories[2]
        self.assertEqual([s.name for s in konpo.subcategories], ["日常点検", "月次点検"])
        daily = konpo.subcategories[0]
        self.assertEqual([i.name for i in daily.items], ["コンベア", "古い様式", "台車点検"])
        self.assertEqual([u.file_name for u in daily.unmatched], ["旧_検査記録.xlsx"])
        self.assertEqual(inv.total_files, 7)   # 対象拡張子のファイル数（VBA版の「検出」）
        self.assertEqual(inv.listed_files, 6)
        self.assertEqual(daily.items[0].ext, "xlsm")

    def test_ids_are_stable_and_resolvable(self):
        inv1 = scan_folder(self.root, ["xlsx", "xlsm", "xls"], True)
        inv2 = scan_folder(self.root, ["xlsx", "xlsm", "xls"], True)
        self.assertEqual(sorted(inv1.by_id), sorted(inv2.by_id))
        item = inv1.categories[2].subcategories[0].items[2]
        self.assertEqual(item.id, make_item_id("梱包", "日常点検", "梱包_日常点検_台車点検.xlsx"))
        self.assertTrue(os.path.isfile(inv1.find(item.id).path))

    def test_missing_root(self):
        inv = scan_folder(os.path.join(self.root, "not_exists"), ["xlsx"], True)
        self.assertFalse(inv.root_exists)
        self.assertEqual(inv.categories, [])

    def test_new_file_is_reflected_on_rescan(self):
        make_tree(self.root, ["梱包/月次点検/梱包_月次点検_非常灯.xlsx"])
        inv = scan_folder(self.root, ["xlsx"], True)
        monthly = inv.categories[2].subcategories[1]
        self.assertIn("非常灯", [i.name for i in monthly.items])


if __name__ == "__main__":
    unittest.main()
