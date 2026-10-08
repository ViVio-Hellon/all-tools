"""保存処理の進み具合 (v3.94.0)

    保存処理にもプログレスを表示し進捗がわかるようにしてください

取り込みにだけあった棒を、**共有へ保存**と**マスタの1行を直す**にも出す。
どちらも終わるまで返らないので、進み具合だけ別に置き(`job_progress`)、
画面が `GET /api/progress` を見に来る(`static/js/progress.js`)。

    共有へ保存 … [1/5] 送る前に確かめています(直ごと)
                 [2/5] 共有へ送っています(ページごと。25ページずつ束ねる)
                 [3/5] 直ごとの集計を共有へ送っています(直ごと)
                 [4/5] 集計CSV・履歴・標準作業時間を写しています
                 [5/5] 月替わりを確かめています
    マスタ     … [1/2] 元のファイルに書いています → [2/2] 読み直しています
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou import job_progress                              # noqa: E402
from nippou.logic import progress as logic                   # noqa: E402
from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"


class WordingTests(unittest.TestCase):
    def test_push_phases_and_units(self) -> None:
        now = logic.Progress(running=True, job=logic.JOB_PUSH,
                             phase=logic.PHASE_SEND, done=3, total=4)
        self.assertEqual(now.headline, "[2/5] 共有へ送っています 3/4ページ (75%)")
        check = logic.Progress(running=True, job=logic.JOB_PUSH,
                               phase=logic.PHASE_CHECK, done=1, total=2)
        self.assertIn("[1/5] 送る前に確かめています 1/2直", check.headline)
        self.assertEqual(now.title, "共有へ保存しています")
        # **集計を送る段も数える**(以前は「2/4」のまま棒が動かなかった)
        summary = logic.Progress(running=True, job=logic.JOB_PUSH,
                                 phase=logic.PHASE_SEND_SUMMARY, done=4, total=10)
        self.assertEqual(summary.headline,
                         "[3/5] 直ごとの集計を共有へ送っています 4/10直 (40%)")

    def test_中止は送っている段だけ(self) -> None:
        sending = logic.Progress(running=True, job=logic.JOB_PUSH, phase=logic.PHASE_SEND)
        self.assertTrue(sending.as_dict()["can_stop"])
        checking = logic.Progress(running=True, job=logic.JOB_PUSH, phase=logic.PHASE_CHECK)
        self.assertFalse(checking.as_dict()["can_stop"])
        stopping = logic.Progress(running=True, job=logic.JOB_PUSH,
                                  phase=logic.PHASE_SEND, stopping=True)
        self.assertFalse(stopping.as_dict()["can_stop"])
        self.assertIn("中止しています", stopping.note)

    def test_master_has_two_steps(self) -> None:
        now = logic.Progress(running=True, job=logic.JOB_MASTER,
                             phase=logic.PHASE_RELOAD)
        self.assertEqual(now.headline, "[2/2] 書いた内容を読み直しています")

    def test_before_the_first_phase_says_what_job(self) -> None:
        now = logic.Progress(running=True, job=logic.JOB_PUSH)
        self.assertEqual(now.headline, "共有へ保存しています")

    def test_import_is_unchanged(self) -> None:
        """取り込みの言い方は前のまま(段は4つ、「ページ」)。"""
        now = logic.Progress(running=True, phase=logic.PHASE_WRITE,
                             done=120, total=340)
        self.assertIn("[2/4] 日報を入れています 120/340ページ (35%)", now.headline)

    def test_as_dict_carries_job_and_title(self) -> None:
        data = logic.Progress(running=True, job=logic.JOB_MASTER).as_dict()
        self.assertEqual((data["job"], data["title"], data["steps"]),
                         (logic.JOB_MASTER, "マスタに書いています", 2))


class HolderTests(unittest.TestCase):
    def setUp(self) -> None:
        job_progress.reset()
        self.addCleanup(job_progress.reset)

    def test_job_is_kept_across_steps(self) -> None:
        with job_progress.watching(job=logic.JOB_PUSH):
            job_progress.step(phase=logic.PHASE_SEND, total=2)
            job_progress.step(done=1)
            self.assertEqual(job_progress.snapshot().job, logic.JOB_PUSH)
        self.assertFalse(job_progress.snapshot().running)

    def test_services_do_nothing_when_nobody_watches(self) -> None:
        """確かめる・送るは画面の外からも呼ばれる。**走っていなければ何もしない。**"""
        job_progress.step(phase=logic.PHASE_SEND, total=9)
        self.assertFalse(job_progress.snapshot().running)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class PushProgressTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 共有の置き場所も一時フォルダへ(**テストは自分の一時フォルダの外へ書かない**)
        from nippou import config, user_settings
        (self.tmp / "shared").mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.tmp / "shared")})

    def pending(self):
        from nippou.db.models import HeaderRecord
        return [HeaderRecord(report_date="2026年9月29日", line="L-1", shift=s, page=p)
                for s in ("1直", "2直") for p in (1, 2)]

    def test_下見_多ければ確かめる(self) -> None:
        """**数が多いときは押す前に訊く**(画面は `confirmPush`)。"""
        from nippou.db import repository
        from nippou.db.models import HeaderRecord

        many = [HeaderRecord(report_date=f"2026年9月{d}日", line="L-1", shift="1直", page=p)
                for d in range(1, 31) for p in (1, 2, 3, 4)]
        with mock.patch.object(repository.NippouRepository, "pending_sync_headers",
                               lambda self: list(many)):
            body = self.get("/api/settings/push/plan").get_json()
        self.assertEqual(body["pages"], 120)
        self.assertEqual(body["shifts"], 30)
        self.assertTrue(body["confirm_needed"])
        self.assertEqual((body["first_day"], body["last_day"]),
                         ("2026年9月1日", "2026年9月30日"))
        with mock.patch.object(repository.NippouRepository, "pending_sync_headers",
                               lambda _self: self.pending()):
            body = self.get("/api/settings/push/plan").get_json()
        self.assertFalse(body["confirm_needed"])

    def test_中止は走っていなければ断る(self) -> None:
        self.assertEqual(self.post("/api/progress/stop", {}).status_code, 409)

    def test_画面は押す前に下見を見る(self) -> None:
        progress_js = (JS / "progress.js").read_text(encoding="utf-8")
        self.assertIn("export async function confirmPush()", progress_js)
        self.assertIn('"/api/progress/stop"', progress_js)
        for view in ("views/entry.js", "views/settings.js", "views/review.js"):
            text = (JS / view).read_text(encoding="utf-8")
            self.assertIn("if (!(await confirmPush())) return;", text, view)

    def test_idle_answer(self) -> None:
        body = self.get("/api/progress").get_json()
        self.assertFalse(body["running"])
        self.assertEqual(body["headline"], "")

    def test_push_reports_check_then_send_then_after(self) -> None:
        # **読み直したほうの** job_progress(WebTestCase はモジュールを読み直す)
        from nippou import job_progress as holder
        from nippou.access_bridge import pusher
        from nippou.db import repository
        from nippou.services import shift_check

        seen: list[tuple[str, str, int, int, str]] = []

        def note():
            now = holder.snapshot()
            seen.append((now.job, now.phase, now.done, now.total, now.label))

        def fake_run(repo, d, line, shift, **kw):
            note()
            return shift_check.CheckReport(d, line, shift)

        def fake_push(repo, path, header, runner, summary):
            note()
            summary.succeeded.append(header.key())

        headers = self.pending()
        with mock.patch.object(repository.NippouRepository, "pending_sync_headers",
                               lambda self: list(headers)), \
                mock.patch.object(shift_check, "run", fake_run), \
                mock.patch.object(pusher, "_push_one", fake_push), \
                mock.patch.object(pusher, "is_sqlite_target", lambda path: False), \
                mock.patch.object(pusher, "_push_summaries", lambda repo, path: 0):
            res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 200, res.get_json())
        checks = [s for s in seen if s[1] == logic.PHASE_CHECK]
        sends = [s for s in seen if s[1] == logic.PHASE_SEND]
        self.assertEqual([s[0] for s in seen], [logic.JOB_PUSH] * len(seen))
        # 確かめるのは直ごと(2直)、送るのはページごと(4ページ)
        self.assertEqual([(s[2], s[3]) for s in checks], [(0, 2), (1, 2)])
        self.assertEqual([(s[2], s[3]) for s in sends], [(0, 4), (1, 4), (2, 4), (3, 4)])
        self.assertEqual(sends[0][4], "2026年9月29日 L-1 1直 1ページ")
        # 終われば下ろす(棒が出たままにならない)
        self.assertFalse(self.get("/api/progress").get_json()["running"])

    def test_refused_push_also_lowers_the_bar(self) -> None:
        from nippou.db import repository
        from nippou.logic.save_checks import Finding
        from nippou.services import shift_check

        def refuse(repo, d, line, shift, **kw):
            report = shift_check.CheckReport(d, line, shift)
            report.findings.append(Finding("x", "直すところ"))
            return report

        headers = self.pending()
        with mock.patch.object(repository.NippouRepository, "pending_sync_headers",
                               lambda self: list(headers)), \
                mock.patch.object(shift_check, "run", refuse):
            res = self.post("/api/settings/push", {})
        self.assertEqual(res.status_code, 422)
        self.assertFalse(self.get("/api/progress").get_json()["running"])


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class MasterProgressTests(WebTestCase):
    def test_write_then_reload(self) -> None:
        from nippou import job_progress as holder
        from nippou import master_admin

        seen: list[tuple[str, str, str]] = []

        def fake_save(*args, **kwargs):
            now = holder.snapshot()
            seen.append((now.job, now.phase, now.label))
            return master_admin.Result(True, "直しました")

        real_after = master_admin.after_write

        def fake_after(result, file_key, table, repo):
            now = holder.snapshot()
            seen.append((now.job, now.phase, now.label))
            return real_after(result, file_key, table, repo)

        with mock.patch.object(master_admin, "save_row", fake_save), \
                mock.patch.object(master_admin, "after_write", fake_after):
            res = self.post("/api/master/row/save",
                            {"file": "transmission", "table": "時間用",
                             "key": 1, "values": {"開始": "07:00"}})
        self.assertIn(res.status_code, (200, 404, 422))
        self.assertEqual(seen, [
            (logic.JOB_MASTER, logic.PHASE_MASTER_WRITE, "transmission / 時間用"),
            (logic.JOB_MASTER, logic.PHASE_RELOAD, "transmission / 時間用")])
        self.assertFalse(self.get("/api/progress").get_json()["running"])


class ScreenWiringTests(unittest.TestCase):
    """画面は投げたあと `/api/progress` を見に来て、**終われば必ず消す。**"""

    def test_progress_module_polls_the_holder(self) -> None:
        text = (JS / "progress.js").read_text(encoding="utf-8")
        self.assertIn('background.get("/api/progress")', text)
        self.assertIn("SHOW_AFTER_MS", text)          # 一瞬で終わるものには出さない

    def test_every_save_button_watches_and_stops(self) -> None:
        for name, label in (("views/settings.js", "共有へ保存しています"),
                            ("views/entry.js", "共有へ保存しています"),
                            ("views/review.js", "共有へ保存しています"),
                            ("views/master.js", "マスタに書いています")):
            with self.subTest(name=name):
                text = (JS / name).read_text(encoding="utf-8")
                self.assertIn('from "../progress.js"', text)
                self.assertIn(f'watchJob("{label}")', text)
                self.assertRegex(text, r"finally \{\s*(?://[^\n]*\n\s*)?stop\(\)")


if __name__ == "__main__":
    unittest.main()
