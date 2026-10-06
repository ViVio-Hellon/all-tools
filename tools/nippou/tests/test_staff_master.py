import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import staff_master
from nippou.access_bridge.runner import ScriptResult


class FakeCsvRunner:
    """test_gw_master.FakeCsvRunner と同じ考え方: 生成VBScript内の
    SaveToFile パスへ指定したCSVを書き込んでから成功を返す。"""

    def __init__(self, csv_text: str) -> None:
        self.csv_text = csv_text

    def run(self, script_text: str) -> ScriptResult:
        match = re.search(r'SaveToFile\s+"((?:[^"]|"")*)"', script_text)
        assert match, "生成スクリプトにSaveToFile呼び出しが見つかりません"
        path = match.group(1).replace('""', '"')
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(self.csv_text)
        return ScriptResult(success=True)


class FakeFailRunner:
    def run(self, script_text: str) -> ScriptResult:
        return ScriptResult(success=False, err_desc="接続失敗")


# 提出された「梱包資材マスタ.accdb」の班員名簿テーブルの実カラム
# (管理番号/苗字/班/名前/読み/担当ライン)を模したCSV。
STAFF_CSV = (
    "管理番号,苗字,班,名前,読み,担当ライン\r\n"
    '"9","井上","D","井上拓実","イノウエ","コイル"\r\n'
    '"28","佐藤貴","A","佐藤貴紀","サトウタ","コイル"\r\n'
    '"1","　","昼","日軽金応援","　",\r\n'
    '"19","金谷",,"金谷幸香","カナタニ",\r\n'
)


class LoadStaffMembersTests(unittest.TestCase):
    def test_parses_rows_and_skips_blank_names(self) -> None:
        members = staff_master.load_staff_members(master_path=Path("dummy.accdb"), runner=FakeCsvRunner(STAFF_CSV))
        names = [m.name for m in members]
        self.assertIn("井上拓実", names)
        self.assertIn("佐藤貴紀", names)
        self.assertIn("日軽金応援", names)
        self.assertIn("金谷幸香", names)  # 班が空欄でも読み込みはされる(グルーピング時にスキップ)

        by_name = {m.name: m for m in members}
        self.assertEqual(by_name["井上拓実"].team, "D")
        self.assertEqual(by_name["井上拓実"].reading, "イノウエ")
        self.assertEqual(by_name["金谷幸香"].team, "")

    def test_returns_empty_list_on_failure_without_raising(self) -> None:
        members = staff_master.load_staff_members(master_path=Path("dummy.accdb"), runner=FakeFailRunner())
        self.assertEqual(members, [])


if __name__ == "__main__":
    unittest.main()
