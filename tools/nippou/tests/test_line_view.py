"""集計管理・集計・グラフで Administrator はラインを選べる (v4.29.0)

    集計管理:アクセス権限がAdministratorの場合はどのラインの集計も見れるようにしてください
    ただし混ぜないでください あくまでラインごとに見れるようにするだけです
    表示 の隣に ライン を追加しAdministratorの場合だけ表示し
    コンボボックスでライン選択できるようにし表示データもそれに追従させてください

【約束】
    ・選ぶ欄は Administrator の PC だけ。ほかの PC は名指ししてもこの端末のライン
    ・この端末のラインは手元、ほかのラインは控え(LocalBackup)から
    ・**混ぜない**: 選んだラインの行だけ。この端末の行は入らない
    ・控えには書かない(集計はメモリーの上で作る)
    ・控えが無ければ、そう言う(前のラインの表を出したままにしない)
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import config  # noqa: E402
from nippou.db.connection import connect  # noqa: E402
from nippou.db.models import DetailRecord, HeaderRecord  # noqa: E402
from nippou.db.repository import NippouRepository  # noqa: E402
from nippou.services import local_backup as lb  # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年10月1日"
SPAN = {"start": "2026-10-01", "end": "2026-10-01"}


def save(repo: NippouRepository, *, line: str, lot: str, shift: str = "1直") -> None:
    key = dict(report_date=DAY, line=line, shift=shift, page=1)
    repo.save(HeaderRecord(**key, worker="山田"),
              [DetailRecord(**key, row_no=1, lot=lot, siz="8.000×1528.0×3053.0",
                            kz="08", kh="00", sz="10", sh="00", hit="2", mai="10", tut="1",
                            con="10", wei="100", tim="120", others1="H176", others4="1P0001")])


def lots_of(body: dict) -> str:
    """表に出ている字を全部(どのロットが入っているかを見る)。"""
    return json.dumps(body["tables"], ensure_ascii=False)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class LineViewWebTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import user_settings
        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})
        lb.reset()
        self.addCleanup(lb.reset)
        # この端末(L-1)の行
        save(self.repo(), line="L-1", lot="OWNLOT1")

    def administrator(self) -> None:
        from nippou.services import access_rights
        patcher = patch.object(access_rights, "is_administrator", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def backup_of(self, line: str, lot: str) -> Path:
        """そのラインの PC が保存して控えへ写した、を作る(別の PC)。"""
        other = NippouRepository(connect(self.tmp / f"{line}-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        save(other, line=line, lot=lot)
        lb.flush(other, force=True)
        return lb.path_for(line)

    # -- 欄 ---------------------------------------------------------------
    def test_一般の端末にはラインの欄が出ない(self) -> None:
        self.backup_of("HVC", "HVCLOT1")
        self.assertNotIn('id="agg-line"', self.get("/agg").get_data(as_text=True))
        self.assertNotIn('id="graph-line"', self.get("/graph").get_data(as_text=True))

    def test_Administratorには表示の隣にラインの欄が出る(self) -> None:
        self.administrator()
        self.backup_of("HVC", "HVCLOT1")
        for path, sel in (("/agg", "agg-line"), ("/graph", "graph-line")):
            page = self.get(path).get_data(as_text=True)
            self.assertIn(f'id="{sel}"', page, path)
            # 「表示」のすぐ後ろ
            self.assertLess(page.index('id="reload"'), page.index(f'id="{sel}"'), path)
            self.assertLess(page.index(f'id="{sel}"'), page.index('id="csv"'), path)
            # この端末のラインが選ばれていて、控えのあるラインも選べる
            self.assertIn('<option value="L-1" selected>', page, path)
            self.assertIn('<option value="HVC"', page, path)

    # -- 集計管理 ---------------------------------------------------------
    def test_一般の端末は名指ししてもこの端末のライン(self) -> None:
        self.backup_of("HVC", "HVCLOT1")
        body = self.post("/api/agg/tables", {**SPAN, "line": "HVC"}).get_json()
        self.assertEqual(body["line"], "L-1")
        self.assertIn("OWNLOT1", lots_of(body))
        self.assertNotIn("HVCLOT1", lots_of(body))

    def test_Administratorは選んだラインだけを見る_混ぜない(self) -> None:
        self.administrator()
        self.backup_of("HVC", "HVCLOT1")
        body = self.post("/api/agg/tables", {**SPAN, "line": "HVC"}).get_json()
        self.assertEqual(body["line"], "HVC", body)
        self.assertIn("HVCLOT1", lots_of(body))
        self.assertNotIn("OWNLOT1", lots_of(body))
        # この端末のラインへ戻せば、この端末の行だけ
        back = self.post("/api/agg/tables", {**SPAN, "line": "L-1"}).get_json()
        self.assertEqual(back["line"], "L-1")
        self.assertIn("OWNLOT1", lots_of(back))
        self.assertNotIn("HVCLOT1", lots_of(back))

    def test_控えには書かない(self) -> None:
        self.administrator()
        path = self.backup_of("HVC", "HVCLOT1")
        before = path.read_bytes()
        self.post("/api/agg/tables", {**SPAN, "line": "HVC"})
        self.post("/api/graph/dashboard", {**SPAN, "line": "HVC"})
        self.assertEqual(path.read_bytes(), before)
        conn = sqlite3.connect(str(path))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM packing_report").fetchone()[0], 0)
        finally:
            conn.close()

    def test_控えが無いラインはそう言う(self) -> None:
        self.administrator()
        res = self.post("/api/agg/tables", {**SPAN, "line": "AIM"})
        self.assertEqual(res.status_code, 404)
        self.assertIn("控えがありません", res.get_json()["error"]["message"])

    def test_CSVも選んだラインで出す(self) -> None:
        self.administrator()
        self.backup_of("HVC", "HVCLOT1")
        body = self.post("/api/agg/csv", {**SPAN, "line": "HVC"}).get_json()
        self.assertTrue(body["files"], body)
        self.assertTrue(all("HVC" in f for f in body["files"]), body)

    # -- 集計・グラフ -----------------------------------------------------
    def test_グラフもAdministratorは選んだライン(self) -> None:
        self.administrator()
        self.backup_of("HVC", "HVCLOT1")
        body = self.post("/api/graph/dashboard", {**SPAN, "line": "HVC",
                                                  "mode": "period"}).get_json()
        self.assertEqual(body["line"], "HVC", body)
        hist = self.post("/api/graph/history", {**SPAN, "line": "HVC"}).get_json()
        self.assertEqual(hist["line"], "HVC")
        self.assertEqual(len(hist["labels"]), 1)

    def test_グラフも一般の端末はこの端末のライン(self) -> None:
        self.backup_of("HVC", "HVCLOT1")
        body = self.post("/api/graph/dashboard", {**SPAN, "line": "HVC"}).get_json()
        self.assertEqual(body["line"], "L-1")


if __name__ == "__main__":
    unittest.main()
