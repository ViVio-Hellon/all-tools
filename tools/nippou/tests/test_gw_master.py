import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.access_bridge import gw_master
from nippou.access_bridge.runner import ScriptResult
from nippou.logic.gw_calculation import MaterialRate


class FakeCsvRunner:
    """生成されたVBScript内の ``SaveToFile "<path>"`` を正規表現で拾い、
    指定したCSV内容をそこへ書き込んでから成功を返すダミーrunner。
    実際の cscript.exe/ADODB を使わずに importer -> gw_master の
    パース処理を検証するためのテスト用ダブル。"""

    def __init__(self, csv_text: str) -> None:
        self.csv_text = csv_text

    def run(self, script_text: str) -> ScriptResult:
        match = re.search(r'SaveToFile\s+"((?:[^"]|"")*)"', script_text)
        assert match, "生成スクリプトにSaveToFile呼び出しが見つかりません"
        path = match.group(1).replace('""', '"')
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(self.csv_text)
        return ScriptResult(success=True)


class FakeEmptyRunner:
    def run(self, script_text: str) -> ScriptResult:
        return ScriptResult(success=False, err_desc="接続できません")


# 提出された SIKALOT.accdb の実際のカラム名(検査番号/ﾛｯﾄ番号/ｵｰﾀﾞｰ番号/
# 製造材質/製造調質/製造板厚/製造板幅/製造板丈/用途ｺｰﾄﾞ等)を模したCSV。
LOT_CSV = (
    "ﾛｯﾄ番号,ｵｰﾀﾞｰ番号,検査番号,製造材質,製造調質,製造板厚,製造板幅,製造板丈,"
    "用途ｺｰﾄﾞ,設計_設備ｺｰｽ,取引先名,納入先名,送り先名\r\n"
    "R4085Q0,06016307,0000000000,R61S,T651,8,1111,2971.5,K21S,10,"
    "ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ,ﾃﾂﾄﾞｳﾎﾞﾃﾞ-,ｵｸﾘｻｷ\r\n"
)

# 提出された SIKAODR.accdb の実際のカラム名(受注番号/VC_表/VC_裏/
# 取引先名称/納入先名称/送り先名称/合紙/製品単重)を模したCSV。
ORDER_CSV = (
    "受注番号,VC_表,VC_裏,取引先名称,納入先名称,送り先名称,合紙,製品単重\r\n"
    "06016307,K21S-VC-A,,取引先A,納入先A,送り先A,1,71.3\r\n"
)


class SearchLotTests(unittest.TestCase):
    def test_parses_matching_row(self) -> None:
        info = gw_master.search_lot("R4085Q0", master_path=Path("dummy.accdb"), runner=FakeCsvRunner(LOT_CSV))
        self.assertIsNotNone(info)
        self.assertEqual(info.order_no, "06016307")
        self.assertEqual(info.material, "R61S")
        self.assertEqual(info.temper, "T651")
        self.assertAlmostEqual(info.thickness_mm, 8.0)
        self.assertAlmostEqual(info.width_mm, 1111.0)
        self.assertAlmostEqual(info.length_mm, 2971.5)
        self.assertEqual(info.usage_code, "K21S")
        self.assertEqual(info.customer, "ﾆﾎﾝｼﾔﾘﾖｳｾｲｿﾞｳ(ｶ")

    def test_returns_none_when_not_found(self) -> None:
        empty_csv = "ﾛｯﾄ番号,ｵｰﾀﾞｰ番号\r\n"
        info = gw_master.search_lot("NOPE", master_path=Path("dummy.accdb"), runner=FakeCsvRunner(empty_csv))
        self.assertIsNone(info)

    def test_returns_none_on_connection_failure_without_raising(self) -> None:
        info = gw_master.search_lot("R4085Q0", master_path=Path("dummy.accdb"), runner=FakeEmptyRunner())
        self.assertIsNone(info)


class SearchOrderTests(unittest.TestCase):
    def test_parses_matching_row(self) -> None:
        info = gw_master.search_order("06016307", master_path=Path("dummy.accdb"), runner=FakeCsvRunner(ORDER_CSV))
        self.assertIsNotNone(info)
        self.assertEqual(info.vc_front, "K21S-VC-A")
        self.assertEqual(info.vc_back, "")
        self.assertTrue(info.has_interleaf)
        self.assertAlmostEqual(info.unit_weight_kg, 71.3)
        self.assertEqual(info.customer, "取引先A")

    def test_interleaf_false_when_not_one(self) -> None:
        csv_text = "受注番号,VC_表,VC_裏,取引先名称,納入先名称,送り先名称,合紙,製品単重\r\n06016307,,,,,,0,0\r\n"
        info = gw_master.search_order("06016307", master_path=Path("dummy.accdb"), runner=FakeCsvRunner(csv_text))
        self.assertFalse(info.has_interleaf)


class MaterialRateLookupTests(unittest.TestCase):
    def test_lookup_returns_matching_rate(self) -> None:
        rows = [
            {"梱包資材名": "ダンプレート", "単位質量": "1.2", "係数": "1.0"},
            {"梱包資材名": "外装紙", "単位質量": "0.8", "係数": "1.1"},
        ]
        rate = gw_master.lookup_material_rate(rows, "外装紙")
        self.assertEqual(rate, MaterialRate(unit_mass=0.8, coefficient=1.1))

    def test_lookup_returns_zero_rate_when_not_found(self) -> None:
        rate = gw_master.lookup_material_rate([], "存在しない資材")
        self.assertEqual(rate.unit_mass, 0.0)
        self.assertEqual(rate.coefficient, 0.0)


class VcProductNameTests(unittest.TestCase):
    def test_lists_non_blank_names(self) -> None:
        rows = [{"品名": "K21S-VC-A"}, {"品名": ""}, {"品名": "K21S-VC-B"}]
        self.assertEqual(gw_master.list_vc_product_names(rows), ["K21S-VC-A", "K21S-VC-B"])

    def test_lookup_vc_rate(self) -> None:
        # 実際の VC重量 マスタは 品名/単位質量/係数 の構成(資材重量マスタと同じ)。
        rows = [{"品名": "K21S-VC-A", "単位質量": "1.5", "係数": "1.1"}]
        rate = gw_master.lookup_vc_rate(rows, "K21S-VC-A")
        self.assertAlmostEqual(rate.unit_mass, 1.5)
        self.assertAlmostEqual(rate.coefficient, 1.1)

        missing = gw_master.lookup_vc_rate(rows, "不明")
        self.assertEqual(missing.unit_mass, 0.0)
        self.assertEqual(missing.coefficient, 0.0)


if __name__ == "__main__":
    unittest.main()
