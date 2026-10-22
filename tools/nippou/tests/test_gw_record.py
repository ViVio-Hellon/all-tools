"""GW計算の記録 ── どの版で・どの取得元の・いつ時点の資材マスタで計算したか (v4.44.0)

    GW計算にソフトの版、どの取得元の、いつ時点の資材マスタで計算したか まで残して下さい
    (coil-packing-tools のペナラベルがその仕組みを使っているので参照)

【約束】
    ・「計算」を押すたび・印刷の画面を開くたびに1行、月ごとのファイルへ足す(消さない・書き換えない)
    ・残すのは 版(日報・日報複合ツール・中身の印)・資材マスタの取得元と場所・**ファイルの更新日時(いつ時点か)**・
      読んだ時刻・使った単位質量×係数・PC とライン・何を計算したか(LotNo・GW など)
    ・画面(計算の結果の下)と紙(下の段)にも同じ素性を出す。「計算の記録」の画面で一覧を見る
    ・出力先を設定で決めてあれば、そこにも PC ごとのファイルで書く
    ・(v4.44.1)形式は CSV(既定。Excel で開ける見出し付き・BOM 付き)・JSON Lines・両方 から選ぶ(管理者モード)。
      記録の画面はどちらの形式も読む(形式を変えても前の記録が見える)
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
        """この端末の CSV の行(見出し → 字)。"""
        import csv

        out = []
        for f in sorted(gw_record.local_folder().glob("GW計算記録_*.csv")):
            with open(f, encoding="utf-8-sig", newline="") as fh:
                out.extend(csv.DictReader(fh))
        return out

    def json_lines(self) -> list[dict]:
        files = sorted(gw_record.local_folder().glob("GW計算記録_*.jsonl"))
        return [json.loads(t) for f in files for t in f.read_text(encoding="utf-8").splitlines()]

    def choose(self, fmt: str) -> None:
        from nippou import user_settings
        from nippou.config import KEY_GW_RECORD_FORMAT
        user_settings.save(KEY_GW_RECORD_FORMAT, fmt)

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
        self.assertEqual(line["種類"], "計算")
        self.assertEqual(line["資材マスタの時点"], "2026-10-20 14:03:11")
        self.assertEqual(line["資材マスタの取得元"], "sqlite3")
        self.assertEqual(line["資材マスタの場所"], str(self.master()))
        self.assertEqual(line["LotNo"], "N7131T0")
        self.assertEqual(line["GW(kg)"], f'{body["gross_weight"]:.2f}')
        self.assertEqual(line["日報の版"], app_config.version())
        # 使った値(マスタが後で直されても、何で計算したかが分かる)。使わない資材は入れない
        self.assertIn("ダンプレート 0.25×1.0", line["使った値(単位質量×係数)"])
        self.assertIn("帯鉄シール無 0.077×1.0", line["使った値(単位質量×係数)"])
        self.assertNotIn("合紙", line["使った値(単位質量×係数)"])
        self.assertEqual(self.json_lines(), [], "既定は CSV だけ")

    def test_CSVはExcelで開ける形_見出しは1回_BOM付き(self) -> None:
        self.post("/api/gw/calculate", payload())
        self.post("/api/gw/calculate", payload())
        path = next(gw_record.local_folder().glob("GW計算記録_*.csv"))
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "BOM が無いと Excel で文字化けする")
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1, "足すたびに BOM を付けない")
        text = raw.decode("utf-8-sig").splitlines()
        self.assertEqual(text[0].split(",")[:3], ["時刻", "種類", "LotNo"])
        self.assertEqual(len(text), 3, "見出し1行 + 2件")

    def test_JSONLinesと両方も選べる(self) -> None:
        self.choose("jsonl")
        self.post("/api/gw/calculate", payload())
        self.assertEqual(len(self.json_lines()), 1)
        self.assertEqual(self.lines(), [])
        self.choose("both")
        self.post("/api/gw/calculate", payload())
        self.assertEqual(len(self.json_lines()), 2)
        self.assertEqual(len(self.lines()), 1)
        # 記録の画面は両方を読み、同じ記録は1つにする(3件 = jsonl 1 + 両方 1)
        page = self.get("/gw/records").get_data(as_text=True)
        self.assertEqual(page.count("<td class=\"nowrap\">計算"), 2, page)

    def test_形式は管理者モードで変える(self) -> None:
        res = self.post("/api/settings/gw-record-format", {"format": "jsonl"})
        self.assertEqual(res.status_code, 403)
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(self.post("/api/settings/gw-record-format", {"format": "xml"}).status_code, 400)
        body = self.post("/api/settings/gw-record-format", {"format": "both"}).get_json()
        self.assertEqual(body["format"], "both")
        self.assertEqual(gw_record.record_format(), "both")
        self.assertIn('id="gw-record-setting"', self.get("/settings").get_data(as_text=True))

    def test_マスタが差し替わればいつ時点かも変わる(self) -> None:
        os.utime(self.master(), (datetime(2026, 10, 20, 8, 0, 0).timestamp(),) * 2)
        self.post("/api/gw/calculate", payload())
        os.utime(self.master(), (datetime(2026, 10, 21, 9, 30, 0).timestamp(),) * 2)
        self.post("/api/gw/calculate", payload())
        self.assertEqual([x["資材マスタの時点"] for x in self.lines()],
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
        self.assertEqual((line["種類"], line["梱包数"]), ("印刷画面", "3"))

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
        found = list(out.rglob("GW計算記録_*.csv"))
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
