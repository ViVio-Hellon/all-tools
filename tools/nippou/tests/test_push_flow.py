"""共有へ保存の流れを1本に ── 手で押す「共有へ保存」と自動確定 (v4.39.2)

【利用者から】

    手で押す・自動確定を1本にする
    自動確定も月替わりの書き出しを行う

前は「送る → 後始末」がそれぞれに書いてあり、自動確定だけ
- 送っている途中で閉じない守り(`running`)が無かった
- 紙(印刷用HTML)を残していなかった
- 月替わりの書き出しをしていなかった
いまは `services/push_flow` の1か所を両方が通る。ここでは
(1) 自動確定でも紙・月替わり・守りが効く (2) 手で押したときと後に残るものが同じ
(3) 後始末の1つが失敗しても残りは走り、送れたことは変わらない (4) サービスが画面の道に頼らない
を確かめる。
"""
from __future__ import annotations

import re
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON  # noqa: E402
from tests import test_scenario_shift_flow as flow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class LayeringTests(unittest.TestCase):
    def test_サービスは画面の道とFlaskを読まない(self) -> None:
        """自動確定(サービス)から紙を残せなかったのは、紙の処理が画面の道の中にあったから。"""
        bad = []
        for path in sorted((ROOT / "nippou" / "services").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(from app\b|import app\b|from flask\b|import flask\b)", text, re.M):
                bad.append(path.name)
        self.assertEqual(bad, [])


class AfterPushStepsTests(unittest.TestCase):
    """後始末のどれかが失敗しても、残りは走る。"""

    def test_1つが失敗しても残りは走る(self) -> None:
        from nippou.access_bridge import pusher
        from nippou.services import push_flow

        summary = pusher.PushSummary(succeeded=[("2026年10月5日", "L-1", "1直", 1)])
        steps = ["second_output.after_push", "standard_time.after_push",
                 "paper_files.after_push", "push_flow.rollover_after_push"]
        for broken in steps:
            with self.subTest(broken=broken):
                called: list[str] = []

                def fake(name):
                    def run(*_a, **_k):
                        called.append(name)
                        if name == broken:
                            raise RuntimeError("壊れた")
                        return [] if name.endswith("rollover_after_push") else (
                            "" if name.startswith("paper") else None)
                    return run

                history: list[str] = []
                with patch("nippou.services.second_output.after_push", side_effect=fake(steps[0])), \
                        patch("nippou.services.standard_time.after_push", side_effect=fake(steps[1])), \
                        patch("nippou.services.paper_files.after_push", side_effect=fake(steps[2])), \
                        patch("nippou.services.push_flow.rollover_after_push", side_effect=fake(steps[3])):
                    push_flow.after_push(None, summary, history=lambda: history.append("履歴"))
                self.assertEqual(history, ["履歴"])
                self.assertEqual(called, steps, "落ちた段のあとも最後まで走る")


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class AutoAndManualSameFlowTests(flow.ScenarioCase):
    def output(self) -> Path:
        return self.tmp / "出力"

    def papers(self, day: str) -> list[str]:
        return sorted(p.name for p in self.output().rglob(f"印刷_{day}_*.html"))

    def history(self) -> list[tuple]:
        return [(r["report_date"], r["shift"], r["result"]) for r in self.repo().push_history()]

    def test_自動確定でも送った直の紙を残す(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(flow.clean_shift_rows("1直", "A"))
        done = self.close_tick("2026-10-05 14:55")
        self.assertEqual(done["pushed"], 1, done)
        self.assertEqual(self.papers("2026年10月5日"), ["印刷_2026年10月5日_L-1_1直_1.html"])
        self.assertIn("印刷用HTML", done["message"])
        self.assertIn("印刷用HTML", done["after_note"])

    def test_自動確定も送っているあいだは終わらせない(self) -> None:
        from nippou import running
        from nippou.access_bridge import pusher

        seen: list[list[str]] = []
        real = pusher.push_pending

        def watch(*args, **kwargs):
            seen.append(running.labels())
            return real(*args, **kwargs)

        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(flow.clean_shift_rows("1直", "A"))
        with patch.object(pusher, "push_pending", side_effect=watch):
            done = self.close_tick("2026-10-05 14:55")
        self.assertEqual(done["pushed"], 1)
        self.assertTrue(seen and "共有への保存" in seen[0], seen)
        self.assertEqual(running.labels(), [], "終わったら名札を外す")

    def test_手で押したときと後に残るものが同じ(self) -> None:
        # 10/5 は手で押す、10/6 は自動確定。同じ中身を打つ
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(flow.clean_shift_rows("1直", "A"))
        manual = self.push_now(worker="山田")
        self.assertEqual(manual.status_code, 200, manual.get_json())
        self.at("2026-10-05 14:56")
        self.close_tick("2026-10-05 14:56")                 # 済んでいるので何もしない
        self.next_shift("2026-10-06 07:05", "山田")
        self.at("2026-10-06 14:30")
        self.save(flow.clean_shift_rows("1直", "A"))
        auto = self.close_tick("2026-10-06 14:55")
        self.assertEqual(auto["pushed"], 1, auto)
        # 紙
        self.assertEqual(len(self.papers("2026年10月5日")), 1)
        self.assertEqual(len(self.papers("2026年10月6日")), 1)
        # 共有の日報データ
        lots = self.shared_lots()
        self.assertEqual(lots[("2026年10月5日", "1直", 1)], lots[("2026年10月6日", "1直", 1)])
        # 共有保存の履歴(送れた直ごとに1行、同じ結果)
        hist = self.history()
        self.assertEqual([r[2] for r in hist if r[0] == "2026年10月5日"],
                         [r[2] for r in hist if r[0] == "2026年10月6日"])
        # 標準作業時間の実績
        from nippou.config import SETTINGS
        conn = sqlite3.connect(SETTINGS.standard_time_db_path)
        try:
            counts = dict(conn.execute('SELECT 報告日, COUNT(*) FROM "T_作業実績" GROUP BY 報告日'))
        finally:
            conn.close()
        self.assertEqual(counts.get("2026年10月5日"), counts.get("2026年10月6日"), counts)
        self.assertEqual(self.dirty_keys(), set())

    def test_自動確定でも月替わりを書き出す(self) -> None:
        """9/30 の3直を、10/1 の朝の自動確定(残り5分)で送る ── 9月に残りが無くなるので書き出す。"""
        self.start_shift("2026-09-30 22:55", "夜勤")
        self.at("2026-10-01 06:30")
        self.save(flow.clean_shift_rows("3直", "N"))
        done = self.close_tick("2026-10-01 06:55")
        self.assertEqual(done["pushed"], 1, done)
        folder = self.tmp / "monthly" / "L-1" / "2026.09"
        self.assertTrue(any(folder.glob("*.csv")), "9月を書き出していない")
        self.assertIn((2026, 9), self.repo().settled_months())
        self.assertIn("2026年9月", done["message"])

    def test_送れなかったときは紙も月替わりも残さない(self) -> None:
        self.shared_offline()
        self.start_shift("2026-09-30 22:55", "夜勤")
        self.at("2026-10-01 06:30")
        self.save(flow.clean_shift_rows("3直", "N"))
        done = self.close_tick("2026-10-01 06:55")
        self.assertEqual(done["pushed"], 0)
        self.assertEqual(self.papers("2026年9月30日"), [])
        self.assertFalse((self.tmp / "monthly").exists())


for _name in [n for n in dir(flow.ScenarioCase) if n.startswith("test_")]:
    setattr(AutoAndManualSameFlowTests, _name, None)


if __name__ == "__main__":
    unittest.main()
