"""GW計算の記録 ── どの版で・どの取得元の・いつ時点の資材マスタで計算したか (v4.44.0)

    GW計算にソフトの版、どの取得元の、いつ時点の資材マスタで計算したか まで残して下さい
    (coil-packing-tools のペナラベルがその仕組みを使っているので参照)

【約束】
    ・「計算」を押すたび・印刷の画面を開くたびに1行、月ごとのファイルへ足す(消さない・書き換えない)
    ・残すのは 版(日報・日報複合ツール・中身の印)・資材マスタの取得元と場所・**ファイルの更新日時(いつ時点か)**・
      読んだ時刻・使った単位質量×係数・PC とライン・何を計算したか(LotNo・GW など)
    ・画面(計算の結果の下)と紙(下の段)にも同じ素性を出す。「計算の記録」の画面で一覧を見る
    ・出力先を設定で決めてあれば、そこにも PC ごとのファイルで書く
    ・書けなくても計算・印刷は止めない
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.services import gw_record  # noqa: E402
from tests._gw_master import FILE_NAME, GwWebTestCase  # noqa: E402
from tests.test_gw_print import INPUTS, _query  # noqa: E402


def payload() -> dict:
    body = {k: v for k, v in INPUTS.items() if k != "flags"}
    used = set(INPUTS["flags"].split(","))
    body["flags"] = {name: name in used for name in (
        "dunplate", "outer_paper", "interleaf", "band", "poly_sheet", "angle", "hardboard")}
    return body


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_取得元といつ時点かはファイルの更新日時(self) -> None:
        path = self.tmp / FILE_NAME
        path.write_bytes(b"x" * 10)
        os.utime(path, (datetime(2026, 10, 20, 14, 3, 11).timestamp(),) * 2)
        info = gw_record.master_source(path)
        self.assertEqual(info["source"], "sqlite3")
        self.assertEqual(info["path"], str(path))
        self.assertEqual(info["asOf"], "2026-10-20 14:03:11")
        self.assertEqual(info["size"], 10)
        self.assertTrue(info["readAt"])

    def test_Accessの取得元(self) -> None:
        info = gw_record.master_source(self.tmp / "梱包資材マスタ.accdb")
        self.assertIn(info["source"], ("Access", "Access(ODBC)"))
        self.assertEqual(info["asOf"], "")
        self.assertIn("ファイルの日時を読めません", info["problem"])

    def test_版は日報と日報複合ツール(self) -> None:
        from nippou import app_config

        got = gw_record.versions("4.44.0-abcd1234")
        self.assertEqual(got["nippou"], app_config.version())
        self.assertEqual(got["stamp"], "4.44.0-abcd1234")
        # 一式の中(tools/nippou)なら、一式の版も
        root = self.tmp / "set"
        (root / "config").mkdir(parents=True)
        (root / "config" / "tools.json").write_text("{}", encoding="utf-8")
        (root / "config" / "app.json").write_text('{"version": "1.5.50"}', encoding="utf-8")
        (root / "tools" / "nippou").mkdir(parents=True)
        self.assertEqual(gw_record.integrated_version(root / "tools" / "nippou"), "1.5.50")
        self.assertEqual(gw_record.integrated_version(self.tmp), "", "一式の外なら空")

    def test_見出しの形(self) -> None:
        self.assertEqual(gw_record.version_label({"nippou": "4.44.0", "app": "1.5.50"}),
                         "日報 4.44.0 / 日報複合ツール 1.5.50")
        self.assertEqual(gw_record.label({"source": "sqlite3", "path": "X", "asOf": "2026-10-20 14:03:11"}),
                         "sqlite3 X(2026-10-20 14:03:11 時点)")
        self.assertEqual(gw_record.label({}), "(記録なし)")


class GwRecordWebTests(GwWebTestCase):
    def master(self) -> Path:
        from nippou.config import SETTINGS
        return Path(SETTINGS.gw_material_master_path)

    def lines(self) -> list[dict]:
        files = sorted(gw_record.local_folder().glob("GW計算記録_*.jsonl"))
        return [json.loads(t) for f in files for t in f.read_text(encoding="utf-8").splitlines()]

    def test_計算すると版と資材マスタの素性を返して記録に残す(self) -> None:
        from nippou import app_config

        self.assertEqual(self.master().name, FILE_NAME, "試験のマスタを読んでいる")
        os.utime(self.master(), (datetime(2026, 10, 20, 14, 3, 11).timestamp(),) * 2)
        body = self.post("/api/gw/calculate", payload()).get_json()
        record = body["record"]
        self.assertIn(f"日報 {app_config.version()}", record["version_label"])
        self.assertEqual(record["material"]["source"], "sqlite3")
        self.assertEqual(record["material"]["path"], str(self.master()))
        self.assertEqual(record["material"]["asOf"], "2026-10-20 14:03:11")
        self.assertTrue(record["who"]["pc"])

        saved = self.lines()
        self.assertEqual(len(saved), 1)
        line = saved[0]
        self.assertEqual(line["kind"], "計算")
        self.assertEqual(line["material"]["asOf"], "2026-10-20 14:03:11")
        self.assertEqual(line["subject"]["lot_no"], "N7131T0")
        self.assertEqual(line["results"]["gross_weight"], body["gross_weight"])
        # 使った値(マスタが後で直されても、何で計算したかが分かる)
        self.assertEqual(line["rates"]["ダンプレート"], {"単位質量": 0.25, "係数": 1.0})
        self.assertEqual(line["rates"]["帯鉄シール無"], {"単位質量": 0.077, "係数": 1.0})
        self.assertNotIn("合紙", line["rates"], "使わない資材は入れない")
        self.assertEqual(line["version"]["nippou"], app_config.version())

    def test_マスタが差し替わればいつ時点かも変わる(self) -> None:
        os.utime(self.master(), (datetime(2026, 10, 20, 8, 0, 0).timestamp(),) * 2)
        self.post("/api/gw/calculate", payload())
        os.utime(self.master(), (datetime(2026, 10, 21, 9, 30, 0).timestamp(),) * 2)
        self.post("/api/gw/calculate", payload())
        self.assertEqual([x["material"]["asOf"] for x in self.lines()],
                         ["2026-10-20 08:00:00", "2026-10-21 09:30:00"])

    def test_紙にも版と資材マスタの時点を載せて記録に残す(self) -> None:
        from nippou import app_config

        os.utime(self.master(), (datetime(2026, 10, 20, 14, 3, 11).timestamp(),) * 2)
        html = self.get(f"/report/gw?{_query(packs='3')}").get_data(as_text=True)
        self.assertIn('class="provenance"', html)
        self.assertIn(f"日報 {app_config.version()}", html)
        self.assertIn("資材マスタの時点</b> 2026-10-20 14:03:11", html)
        self.assertIn(str(self.master()), html)
        line = self.lines()[-1]
        self.assertEqual((line["kind"], line["packs"]), ("印刷画面", 3))

    def test_断った計算は残さない(self) -> None:
        bad = dict(payload(), count="")
        self.assertEqual(self.post("/api/gw/calculate", bad).status_code, 422)
        self.assertEqual(self.lines(), [])

    def test_計算の記録の画面(self) -> None:
        os.utime(self.master(), (datetime(2026, 10, 20, 14, 3, 11).timestamp(),) * 2)
        self.post("/api/gw/calculate", payload())
        page = self.get("/gw/records").get_data(as_text=True)
        self.assertIn('id="gw-records-table"', page)
        self.assertIn("2026-10-20 14:03:11", page)
        self.assertIn("LotNo N7131T0", page)
        self.assertIn("ダンプレート 0.25×1.0", page)
        self.assertIn(str(gw_record.local_folder()), page)
        self.assertIn('id="gw-records-link"', self.get("/gw").get_data(as_text=True))

    def test_出力先を決めてあればPCごとのファイルにも書く(self) -> None:
        from nippou import user_settings
        from nippou.config import KEY_REPORT_OUT_DIR

        out = self.tmp / "shared-out"
        user_settings.save(KEY_REPORT_OUT_DIR, str(out))
        self.post("/api/gw/calculate", payload())
        found = list(out.rglob("GW計算記録_*.jsonl"))
        self.assertEqual(len(found), 1, found)
        self.assertIn(gw_record.pc_name(), found[0].name)
        self.assertEqual(found[0].parent.name, "GW計算記録")

    def test_書けなくても計算は止めない(self) -> None:
        from unittest import mock

        with mock.patch("builtins.open", side_effect=OSError("書けない")):
            entry = gw_record.record("計算", material={}, rates={}, subject={}, results={})
        self.assertEqual(entry["kind"], "計算")
        self.assertIn("書けない", gw_record.status()["localProblem"])
        with mock.patch.object(gw_record, "append", side_effect=OSError("書けない")):
            res = self.post("/api/gw/calculate", payload())
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
