import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import stop_master
from nippou.access_bridge.runner import ScriptResult


class FakeCsvRunner:
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


REASON_CSV = "内訳,内訳番号\r\n設備故障,1\r\n段取り,2\r\n"


class LoadStopReasonsTests(unittest.TestCase):
    def test_parses_reasons(self) -> None:
        reasons = stop_master.load_stop_reasons(
            "設備停止", master_path=Path("dummy.accdb"), runner=FakeCsvRunner(REASON_CSV)
        )
        self.assertEqual([r.label for r in reasons], ["設備故障", "段取り"])
        self.assertEqual([r.code for r in reasons], ["1", "2"])

    def test_unknown_category_raises(self) -> None:
        with self.assertRaises(ValueError):
            stop_master.load_stop_reasons("不明カテゴリ")

    def test_returns_empty_list_on_failure_without_raising(self) -> None:
        reasons = stop_master.load_stop_reasons(
            "不稼働", master_path=Path("dummy.accdb"), runner=FakeFailRunner()
        )
        self.assertEqual(reasons, [])

    def test_load_all_categories_covers_all_three(self) -> None:
        result = stop_master.load_all_categories(master_path=Path("dummy.accdb"), runner=FakeCsvRunner(REASON_CSV))
        self.assertEqual(set(result.keys()), {"設備停止", "不稼働", "ハンドリング"})
        for reasons in result.values():
            self.assertEqual(len(reasons), 2)


class FindReasonCodeTests(unittest.TestCase):
    def test_finds_matching_code(self) -> None:
        reasons = [stop_master.StopReason(label="設備故障", code="1"), stop_master.StopReason(label="段取り", code="2")]
        self.assertEqual(stop_master.find_reason_code(reasons, "段取り"), "2")

    def test_returns_empty_when_not_found(self) -> None:
        self.assertEqual(stop_master.find_reason_code([], "不明"), "")


if __name__ == "__main__":
    unittest.main()
