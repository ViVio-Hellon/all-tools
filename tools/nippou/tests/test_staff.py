import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic.staff import StaffMember, group_by_team, join_checked_names


class GroupByTeamTests(unittest.TestCase):
    def test_groups_into_known_teams(self) -> None:
        members = [
            StaffMember(name="佐藤貴紀", team="A", reading="サトウタ"),
            StaffMember(name="加賀谷哲志", team="A", reading="カガヤ"),
            StaffMember(name="森智明", team="昼", reading="モリチ"),
            StaffMember(name="岩井", team="丸", reading="イワイ"),
        ]
        groups = group_by_team(members)
        self.assertEqual(set(groups.keys()), {"A", "B", "C", "D", "昼", "丸"})
        # 同じ班(A)の中は読み順(カガヤ→サトウタ)になっていること。
        self.assertEqual(groups["A"], ["加賀谷哲志", "佐藤貴紀"])
        self.assertEqual(groups["昼"], ["森智明"])
        self.assertEqual(groups["丸"], ["岩井"])
        self.assertEqual(groups["B"], [])

    def test_unknown_team_is_skipped(self) -> None:
        # 提出データの実例: 班が空欄、または未定義の値を持つ行はスキップ。
        members = [
            StaffMember(name="金谷幸香", team="", reading="カナタニ"),
            StaffMember(name="正常太郎", team="A", reading="セイジョウ"),
        ]
        groups = group_by_team(members)
        all_names = [name for names in groups.values() for name in names]
        self.assertEqual(all_names, ["正常太郎"])

    def test_reading_order_preserved_within_team_stable_sort(self) -> None:
        members = [
            StaffMember(name="三番目", team="C", reading="み"),
            StaffMember(name="一番目", team="C", reading="あ"),
            StaffMember(name="二番目", team="C", reading="い"),
        ]
        groups = group_by_team(members)
        self.assertEqual(groups["C"], ["一番目", "二番目", "三番目"])


class JoinCheckedNamesTests(unittest.TestCase):
    def test_joins_with_space(self) -> None:
        self.assertEqual(join_checked_names(["山田", "田中", "佐藤"]), "山田 田中 佐藤")

    def test_empty_list_is_empty_string(self) -> None:
        self.assertEqual(join_checked_names([]), "")

    def test_trainee_suffix_appended_to_each_name(self) -> None:
        self.assertEqual(join_checked_names(["山田", "田中"], trainee_suffix="(新人教育)"), "山田(新人教育) 田中(新人教育)")


if __name__ == "__main__":
    unittest.main()
