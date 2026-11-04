"""管理者以外停止モード(`portal/remote_stop.py`・`portal/stop_mode.py`。python-web-tools VER4.8.129・4.8.130 と同じ決め)

現場の決め:
    お願いを出せる      管理者パスワード かつ この端末のアクセス権限に Administrator(大設定)
    止める範囲          お願いを出した PC 以外の、起動中の PC(日報複合ツールと単体の4ツール)
    保存していない・     帯で知らせて利用者に任せる。何も残っていなければまもなく止まる。5分で強制
    処理中               (止める前に打ちかけを置いてもらい、残っていたものを手元の控えに書いて、次の起動で知らせる)
    更新中               お願いを出した PC 以外は起動させない
    ランチャー           同時には使わない(お願いが出ているあいだ、止める入口は理由を返して 3)

本物の子プロセス(ブラウザ版の入口 `start_app.py`)でも、お願い → 止まる・待って強制・起動しない・
解除・共有に届かない、を確かめる(時間は `ALLTOOLS_REMOTE_STOP_TIMES` で短くする)。
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import process_manager
from portal import remote_stop, stop_mode

ROOT = Path(__file__).resolve().parent.parent
COPIES = ("portal/remote_stop.py", "tools/nippou/remote_stop.py", "tools/kanban/remote_stop.py",
          "tools/calendar/remote_stop.py", "tools/inspection/remote_stop.py")


BANDS = tuple(f"tools/{t}/app/static/js/remote_stop_band.js" for t in ("nippou", "kanban", "calendar", "inspection"))


class ParityTests(unittest.TestCase):
    def test_5か所の中身は同じ(self) -> None:
        """4ツールは単体でも動くので入口を読み込めない。同じ中身を5か所に置く(片方だけ直さない)。"""
        base = (ROOT / COPIES[0]).read_bytes()
        for name in COPIES[1:]:
            with self.subTest(name=name):
                self.assertEqual((ROOT / name).read_bytes(), base, f"{name} が portal/remote_stop.py と違う")

    def test_4ツールの帯の台本は同じで_画面に載っている(self) -> None:
        base = (ROOT / BANDS[0]).read_bytes()
        for name in BANDS:
            with self.subTest(name=name):
                self.assertEqual((ROOT / name).read_bytes(), base)
                html = (ROOT / name).parent.parent.parent / "templates" / "base.html"
                self.assertIn("js/remote_stop_band.js", html.read_text(encoding="utf-8"))


class _Base(unittest.TestCase):
    PC = "PC-ADMIN"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.local = self.tmp / "ローカル"
        self.alltools = self.tmp / "AllTools"
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))
        remote_stop.configure(root=lambda: self.share, local_root=self.local, app="日報複合ツール",
                              app_id="portal", hosted=False, version="9.9.9", logger=stop_mode.log)
        for patcher in (
                mock.patch.object(remote_stop, "READ_RETRY_SEC", ()),
                mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": self.PC, "ALLTOOLS_LOGIN_ID": "kanri",
                                             "ALLTOOLS_LOCAL_DIR": str(self.alltools)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        remote_stop.reset()
        self.addCleanup(remote_stop.reset)

    def issue(self, *, password_ok: bool = True, admin: bool = True, reason: str = "更新のため"):
        return remote_stop.issue(password_ok, admin, reason)

    def as_pc(self, name: str):
        return mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": name})


class IssueTests(_Base):
    def test_パスワードとAdministratorの両方が要る(self) -> None:
        self.assertEqual(self.issue(password_ok=False).reason, remote_stop.REFUSE_PASSWORD)
        self.assertEqual(self.issue(admin=False).reason, remote_stop.REFUSE_NOT_ADMIN)
        self.assertFalse(remote_stop.request_path().exists())

    def test_お願いを出す_出ているあいだは重ねて出さない_解除する(self) -> None:
        self.assertTrue(self.issue().ok)
        req = remote_stop.read_request()
        self.assertEqual((req["by_pc"], req["reason"], req["grace_sec"]), (self.PC, "更新のため", 300))
        self.assertEqual(self.issue().reason, remote_stop.REFUSE_ALREADY)
        self.assertTrue(remote_stop.clear(True, True).ok)
        self.assertEqual(remote_stop.clear(True, True).reason, remote_stop.REFUSE_NOT_ACTIVE)
        self.assertIsNone(remote_stop.read_request())
        self.assertEqual(remote_stop.request_path().parent.name, "管理者以外停止")

    def test_読めないお願いは上書きしない(self) -> None:
        remote_stop.folder().mkdir(parents=True)
        remote_stop.request_path().write_text("{壊れている", encoding="utf-8")
        self.assertEqual(self.issue().reason, remote_stop.REFUSE_UNREADABLE)
        self.assertEqual(remote_stop.clear(True, True).reason, remote_stop.REFUSE_UNREADABLE)
        self.assertEqual(remote_stop.request_path().read_text(encoding="utf-8"), "{壊れている")

    def test_共有のフォルダに届かなければ出せない(self) -> None:
        remote_stop.configure(root=lambda: self.share / "無い")
        self.assertEqual(self.issue().reason, remote_stop.REFUSE_NO_FOLDER)


class AdministratorTests(_Base):
    """この端末のアクセス権限に Administrator(共有の DB の表「アクセス権限」の「権限」)。"""

    def make_db(self, rows) -> Path:
        path = self.share / "梱包資材マスタ.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "アクセス権限" (管理番号 INTEGER PRIMARY KEY, ログインID TEXT, PC名 TEXT, '
                     '権限 TEXT, 有効 INTEGER, 備考 TEXT)')
        conn.executemany('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効) VALUES (?, ?, ?, ?)', rows)
        conn.commit()
        conn.close()
        return path

    def check(self):
        with mock.patch.dict(os.environ, {"ALLTOOLS_SHARED_DB_DIR": str(self.share)}), \
                mock.patch("portal.shared_db.configured", return_value=("", "")):
            return stop_mode.administrator()

    def test_この端末の行の権限にAdministratorがあれば(self) -> None:
        self.make_db([("kanri", "PC-ADMIN", "L-1, Administrator", 1), ("", "PC-LINE1", "L-1", 1)])
        self.assertEqual(self.check(), (True, ""))

    def test_ほかの端末の行やライン名だけでは足りない(self) -> None:
        self.make_db([("kanri", "PC-OTHER", "Administrator", 1), ("", "PC-ADMIN", "L-1", 1),
                      ("kanri", "PC-ADMIN", "Administrator", 0), ("", "", "Administrator", 1)])
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertIn("Administrator がありません", why)

    def test_PC名だけの行でも当たる_大文字小文字と全角は問わない(self) -> None:
        self.make_db([("", "ｐｃ-admin", "administrator", 1)])
        self.assertTrue(self.check()[0])

    def test_表が無い_共有のDBが無いときは出せない(self) -> None:
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertIn("読めない", why)
        conn = sqlite3.connect(self.share / "梱包資材マスタ.sqlite3")
        conn.execute("CREATE TABLE x (a)")
        conn.close()
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertIn("「アクセス権限」の表がありません", why)

    def test_入口の画面からは_パスワードが違えば表を読まずに断る(self) -> None:
        with mock.patch("portal.admin_password.verify", return_value=False), \
                mock.patch.object(stop_mode, "administrator") as admin:
            self.assertEqual(stop_mode.issue("x").reason, remote_stop.REFUSE_PASSWORD)
        admin.assert_not_called()
        with mock.patch("portal.admin_password.verify", return_value=True), \
                mock.patch.object(stop_mode, "administrator", return_value=(False, "無い")):
            self.assertEqual(stop_mode.issue("x").reason, remote_stop.REFUSE_NOT_ADMIN)


class StartupTests(_Base):
    def test_更新中はお願いを出したPC以外で起動しない(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            why = remote_stop.startup_block()
        self.assertIn("更新が終わるまで起動できません", why)
        self.assertIn(str(remote_stop.request_path()), why, "解除できないときの逃げ道(名前を変える)")
        self.assertIsNone(remote_stop.startup_block(), "お願いを出した PC は起動できる")

    def test_解除すれば起動できる(self) -> None:
        self.issue()
        remote_stop.clear(True, True)
        with self.as_pc("PC-LINE1"):
            self.assertIsNone(remote_stop.startup_block())

    def test_読めなくても_最後に見たお願いが無ければ起動は止めない(self) -> None:
        remote_stop.folder().mkdir(parents=True)
        remote_stop.request_path().write_text("{壊れている", encoding="utf-8")
        with self.as_pc("PC-LINE1"):
            self.assertIsNone(remote_stop.startup_block())

    def test_読めないとき_最後に見たお願いが出ていれば起動しない(self) -> None:
        """読めないのを「解除された」と取り違えない。覚えは読めた回だけ書く・消す。"""
        self.issue()
        with self.as_pc("PC-LINE1"):
            self.assertIsNotNone(remote_stop.startup_block())          # 見た(覚える)
            remote_stop.request_path().write_text("{書いている途中", encoding="utf-8")
            why = remote_stop.startup_block()
            self.assertIn("解除されたかを確かめられません", why)
            self.assertIn(str(remote_stop.memory_path()), why, "急ぐときの逃げ道")
            # 覚えが古ければ(共有が落ちたまま長い)起動する ── 永久に起動できない、にしない
            with mock.patch.object(remote_stop, "MEMORY_KEEP_SEC", -1):
                self.assertIsNone(remote_stop.startup_block())
            # 読めるようになって解除されていれば、覚えも消える
            remote_stop.request_path().write_text(json.dumps({"active": False}), encoding="utf-8")
            self.assertIsNone(remote_stop.startup_block())
            self.assertFalse(remote_stop.memory_path().exists())

    def test_共有に届かないときは無いとは見ない(self) -> None:
        # 届かない共有のまね: 親がファイル(一覧できない)
        (self.tmp / "ファイル").write_text("x", encoding="utf-8")
        remote_stop.configure(root=lambda: self.tmp / "ファイル" / "共有")
        with self.assertRaises(remote_stop.Unreadable):
            remote_stop.read_request()
        with self.as_pc("PC-LINE1"):
            self.assertIsNone(remote_stop.startup_block(), "最後に見たお願いが無ければ起動する")
        # 本当に無い(親を一覧できる)なら「出ていない」
        remote_stop.configure(root=lambda: self.tmp / "無い共有")
        self.assertIsNone(remote_stop.read_request())

    def test_入口の起動で_理由を出して起動しない(self) -> None:
        """ブラウザ版は理由を出して終わる(Windows の窓・記録)、デスクトップ版は外枠が窓に出す(fatal)。"""
        import start_app
        self.issue()
        with self.as_pc("PC-LINE1"), \
                mock.patch.object(stop_mode, "configure"), \
                mock.patch("portal.instance_guard.claim") as claim, \
                mock.patch("portal.distribution.apply_on_start"), \
                mock.patch.object(start_app, "port_free", return_value=True):
            claim.return_value.ok = True
            with self.assertRaises(start_app.StartupError) as caught:
                start_app.start(open_browser=False)
        self.assertIn("管理者が更新のため", str(caught.exception))
        self.assertIn("別の名前に変えると解除できます", caught.exception.hint)


class StopEntryTests(_Base):
    def run_main(self, argv, running="browser"):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(stop_mode, "configure"), \
                mock.patch("portal.instance_guard.running", return_value=running), \
                mock.patch.object(process_manager, "_read_portal_lock", return_value=None), \
                mock.patch.object(process_manager, "run_stop") as run_stop, \
                mock.patch.object(process_manager, "run_launcher_stop") as run_launcher:
            run_stop.return_value = process_manager.Outcome(0, "止めました")
            run_launcher.return_value = process_manager.Outcome(0, "止めました")
            code = process_manager.main(argv)
        return code, out.getvalue(), run_stop, run_launcher

    def test_お願いが出ているあいだ_止める入口は理由を返して3(self) -> None:
        self.issue()
        for argv in (["/nopause"], ["--force"], ["--launcher"], ["--launcher", "--force"]):
            with self.subTest(argv=argv):
                code, out, run_stop, run_launcher = self.run_main(argv)
                self.assertEqual(code, process_manager.EXIT_BUSY)
                self.assertTrue(out.startswith("止めませんでした: 管理者以外停止モード中"), out)
                self.assertEqual(len(out.strip().splitlines()), 1, "1行目が理由(ランチャーの入口は1行だけ)")
                run_stop.assert_not_called()
                run_launcher.assert_not_called()

    def test_動いていなければ断らない_お願いが無ければいつもどおり(self) -> None:
        self.issue()
        code, _out, run_stop, _ = self.run_main(["/nopause"], running="")
        self.assertEqual(code, 0)
        run_stop.assert_called_once()
        remote_stop.clear(True, True)
        code, _out, run_stop, _ = self.run_main(["/nopause"])
        self.assertEqual(code, 0)
        run_stop.assert_called_once()

    def test_読めなければ断らない(self) -> None:
        remote_stop.folder().mkdir(parents=True)
        remote_stop.request_path().write_text("{", encoding="utf-8")
        code, _out, run_stop, _ = self.run_main(["/nopause"])
        self.assertEqual(code, 0)
        run_stop.assert_called_once()


class WatcherTests(_Base):
    def watcher(self, *, left=None, saved=None, **kw):
        self.now = 1000.0
        self.stopped = []
        self.left = list(left or [])
        return remote_stop.Watcher(lambda: self.stopped.append(True), lambda: self.left,
                                   before_force=(lambda: saved or []), clock=lambda: self.now,
                                   flush=False, **kw)

    def answer(self, w) -> dict:
        return json.loads(w.presence_path().read_text(encoding="utf-8"))

    def test_何も残っていなければ帯を見せてから止まる(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher()
            w.tick()
            self.assertEqual(w.state, remote_stop.STATE_NOTICE)
            self.assertEqual(w.screen_state()["reason"], "更新のため")
            self.assertEqual(self.stopped, [])
            self.now += remote_stop.NOTICE_SEC
            w.tick()
        self.assertEqual(self.stopped, [True])
        self.assertEqual(self.answer(w)["state"], remote_stop.STATE_STOPPED)
        w.leave()
        self.assertTrue(w.presence_path().exists(), "止まった答えは管理者が見るので残す")

    def test_止めると決めたら画面に打ちかけを置いてもらってから止める(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher()
            w.flush = True
            w.tick()
            self.now += remote_stop.NOTICE_SEC
            w.tick()
            self.assertTrue(w.screen_state()["stopping"], "画面はこれを見て打ちかけを置く")
            self.assertEqual(self.stopped, [])
            w.hurry()                          # 画面が置き終えた
            w.tick()
        self.assertEqual(self.stopped, [True])

    def test_保存していないものがあれば待ち_無くなったら止まる(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(left=["送っていない操作 3 件"])
            self.now += remote_stop.NOTICE_SEC + 10
            w.tick()
            self.assertEqual((w.state, w.detail), (remote_stop.STATE_WAITING, "送っていない操作 3 件"))
            self.assertEqual(w.screen_state()["left"], ["送っていない操作 3 件"])
            self.assertEqual(self.stopped, [])
            self.left.clear()
            w.tick()
        self.assertEqual(self.stopped, [True])

    def test_5分たっても止まらなければ控えを書いてから強制で止める(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(left=["打ちかけ", "取り込み"], saved=["打ちかけは手元に置きました"])
            w.tick()
            self.now += 299
            w.tick()
            self.assertEqual(self.stopped, [])
            self.now += 1
            w.tick()
        self.assertEqual(self.stopped, [True])
        answer = self.answer(w)
        self.assertEqual(answer["state"], remote_stop.STATE_FORCED)
        self.assertIn("5分たっても止まらない", answer["detail"])
        self.assertIn("手元に置きました", answer["detail"])
        keeps = list(remote_stop.keep_dir().glob("keep-*.json"))
        self.assertEqual(len(keeps), 1, "この PC の手元の控え(共有には書かない)")
        self.assertFalse(any(self.share.rglob("keep-*")))
        notes = remote_stop.restore_keeps()             # 次の起動
        self.assertEqual(len(notes), 1)
        self.assertIn("打ちかけ、取り込み", notes[0])
        self.assertIn("打ちかけは手元に置きました", notes[0])
        self.assertEqual(remote_stop.restore_keeps(), [], "一度知らせた控えはもう知らせない")
        self.assertTrue(any((remote_stop.keep_dir() / remote_stop.DONE_DIR).glob("keep-*.json")), "消さずに移す")

    def test_壊れた控えは知らせず残す(self) -> None:
        remote_stop.keep_dir().mkdir(parents=True)
        bad = remote_stop.keep_dir() / "keep-20260101-000000-1.json"
        bad.write_text("{壊れ", encoding="utf-8")
        self.assertEqual(remote_stop.restore_keeps(), [])
        self.assertTrue(bad.exists())

    def test_お願いを出したPCは止めない(self) -> None:
        self.issue()
        w = self.watcher()
        w.tick()
        self.now += remote_stop.GRACE_SEC + 1
        w.tick()
        self.assertEqual((self.stopped, w.state), ([], remote_stop.STATE_RUNNING))
        self.assertIsNone(w.screen_state())

    def test_取り下げられたら止めずに続ける(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(left=["打ちかけ"])
            w.tick()
            remote_stop.clear(True, True)
            w.tick()
        self.assertEqual((self.stopped, w.state), ([], remote_stop.STATE_RUNNING))
        self.assertTrue(any("取り下げ" in n for n in remote_stop.take_notes()))

    def test_お願いを読めなくなっても取り下げとは見ない(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(left=["打ちかけ"])
            w.tick()
            remote_stop.request_path().write_text("{書いている途中", encoding="utf-8")
            w.tick()
            self.assertEqual(w.state, remote_stop.STATE_WAITING)
            self.now += remote_stop.GRACE_SEC
            w.tick()
        self.assertEqual(self.stopped, [True])

    def test_数えられなければ残っているとみなす(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = remote_stop.Watcher(lambda: None, lambda: 1 / 0, clock=lambda: 5000.0, flush=False)
            w.tick()
        self.assertEqual(w.state, remote_stop.STATE_WAITING)

    def test_お願いの最中に利用者が自分で終えたら_止まりましたを残す(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher(left=["打ちかけ"])
            w._on_leave = lambda: ["打ちかけ"]
            w.tick()
            w.leave()
        answer = self.answer(w)
        self.assertEqual(answer["state"], remote_stop.STATE_STOPPED)
        self.assertIn("利用者が終了しました", answer["detail"])

    def test_共有に書けないあいだは記録をためない(self) -> None:
        w = self.watcher()
        with mock.patch.object(remote_stop, "write_json", return_value=False) as write, \
                self.assertLogs("portal.remote_stop", "WARNING") as logs:
            for _ in range(3):
                self.now += remote_stop.BEAT_SEC
                w.tick()
        self.assertEqual(write.call_count, 3)
        self.assertEqual(len([r for r in logs.records if "書けません" in r.getMessage()]), 1)

    def test_止める処理は錠の外で呼ぶ(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = self.watcher()
            w._stop = lambda: self.stopped.append(w._lock.locked())
            w.tick()
            self.now += remote_stop.NOTICE_SEC
            w.tick()
        self.assertEqual(self.stopped, [False])

    def test_ふだんの終わりなら知らせを消す(self) -> None:
        w = self.watcher()
        w.tick()
        self.assertTrue(w.presence_path().exists())
        w.leave()
        self.assertFalse(w.presence_path().exists())


class HostedTests(_Base):
    """日報複合ツールの中のツール(自分では止まらない)と、入口(中のツールを待ってから閉じる)。"""

    def test_中のツールは止めてよいと手元に答え_自分では止まらない(self) -> None:
        self.issue()
        now = [1000.0]
        stopped = []
        remote_stop.configure(app="看板", app_id="kanban", hosted=True)
        with self.as_pc("PC-LINE1"):
            w = remote_stop.Watcher(lambda: stopped.append(1), lambda: ["送っていない操作 2 件"], hosted=True,
                                    clock=lambda: now[0])
            w.tick()
            local = json.loads(w.local_path().read_text(encoding="utf-8"))
            self.assertEqual((local["ready"], local["left"]), (False, ["送っていない操作 2 件"]))
            self.assertTrue(str(w.local_path()).startswith(str(self.alltools)), "日報複合ツールの手元の runtime")
            pending, items = remote_stop.hosted_left(w.request["id"])
            self.assertEqual((pending, items), (True, ["看板: 送っていない操作 2 件"]))
            now[0] += remote_stop.GRACE_SEC
            w.tick()
            self.assertEqual(stopped, [], "入口が閉じるのを待つ")
            self.assertTrue(json.loads(w.local_path().read_text(encoding="utf-8"))["ready"])
            self.assertEqual(remote_stop.hosted_left(w.request["id"]), (False, []))
            self.assertTrue(w.screen_state()["hosted"])
            # 入口が閉じないまま長く残ったら、自分で止まる
            now[0] += remote_stop.ORPHAN_SEC
            w.tick()
        self.assertEqual(stopped, [1])
        w.leave()
        self.assertFalse(w.local_path().exists())

    def test_入口は中のツールが止めてよいと答えるまで止めない(self) -> None:
        self.issue()
        now = [1000.0]
        stopped = []
        with self.as_pc("PC-LINE1"):
            holding = [True]
            w = remote_stop.Watcher(lambda: stopped.append(1), lambda: [], hold=lambda: holding[0],
                                    extra_sec=remote_stop.HOST_EXTRA_SEC, clock=lambda: now[0], flush=False)
            w.tick()
            now[0] += remote_stop.NOTICE_SEC + 1
            w.tick()
            self.assertEqual(stopped, [])
            # 期限(+中のツールが控えを書き終える間)をすぎれば閉じる
            now[0] = 1000.0 + remote_stop.GRACE_SEC + 1
            w.tick()
            self.assertEqual(stopped, [], "中のツールが控えを書き終えるのを待つ")
            now[0] += remote_stop.HOST_EXTRA_SEC
            w.tick()
            self.assertEqual(stopped, [1])

    def test_中のツールが控えを書けないと答えていれば入口も強制で閉じない(self) -> None:
        self.issue()
        folder = remote_stop.local_status_dir()
        folder.mkdir(parents=True)
        rid = remote_stop.read_request()["id"]
        (folder / "kanban-1.json").write_text(json.dumps(
            {"app": "看板", "request_id": rid, "ready": False, "left": [remote_stop.Watcher.KEEP_FAILED],
             "beat_at": time.time()}), encoding="utf-8")
        now = [1000.0]
        stopped = []
        with self.as_pc("PC-LINE1"):
            w = remote_stop.Watcher(lambda: stopped.append(1), lambda: [], flush=False, clock=lambda: now[0],
                                    hold=lambda: remote_stop.hosted_left(rid)[0],
                                    force_ok=lambda: not remote_stop.hosted_keep_failed(rid))
            w.tick()
            now[0] += remote_stop.GRACE_SEC + 1
            w.tick()
            self.assertEqual(stopped, [])
            (folder / "kanban-1.json").unlink()
            w.tick()
        self.assertEqual(stopped, [1])

    def test_古い答えは数えない(self) -> None:
        folder = remote_stop.local_status_dir()
        folder.mkdir(parents=True)
        (folder / "nippou-1.json").write_text(json.dumps(
            {"app": "日報", "request_id": "r", "ready": False, "left": ["x"], "beat_at": 1.0}), encoding="utf-8")
        self.assertEqual(remote_stop.hosted_left("r"), (False, []))


class OverviewTests(_Base):
    def test_各PCの様子を並べる_お願いを出したPCは止めない印(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            remote_stop.Watcher(lambda: None, lambda: ["打ちかけ"], flush=False).tick()
        remote_stop.Watcher(lambda: None, lambda: [], flush=False)._beat(force=True)
        view = remote_stop.overview()
        self.assertTrue(view.available)
        self.assertIn("PC-ADMIN(kanri)", view.request_text)
        rows = {r["pc"]: r for r in view.pcs}
        self.assertEqual(rows["PC-LINE1"]["label"], "利用者を待っています")
        self.assertEqual(rows["PC-LINE1"]["detail"], "打ちかけ")
        self.assertEqual(rows["PC-LINE1"]["app"], "日報複合ツール")
        self.assertIn("お願いを出した PC", rows["PC-ADMIN"]["label"])

    def test_知らせが途絶えたPCは応答なし(self) -> None:
        self.issue()
        remote_stop.presence_dir().mkdir(parents=True, exist_ok=True)
        (remote_stop.presence_dir() / "PC-OLD__a__portal__1.json").write_text(json.dumps(
            {"pc": "PC-OLD", "login": "a", "beat_at": 1.0, "state": "running"}), encoding="utf-8")
        rows = {r["pc"]: r for r in remote_stop.overview().pcs}
        self.assertTrue(rows["PC-OLD"]["stale"])
        self.assertIn("応答なし", rows["PC-OLD"]["label"])

    def test_Administratorでない端末は各PCの知らせを読まない(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            remote_stop.Watcher(lambda: None, lambda: [], flush=False).tick()
        with mock.patch.object(stop_mode, "administrator_cached", return_value=(False, "無い")):
            view = stop_mode.state()
        self.assertIsNotNone(view["request"])
        self.assertEqual(view["pcs"], [])
        self.assertFalse(view["administrator"])


class HoleTests(_Base):
    """穴探し(ユーザーの依頼「管理者以外停止モードに穴がないかもう一度チェック」)で確かめたこと。"""

    def write_request(self, data) -> None:
        remote_stop.folder().mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        remote_stop.request_path().write_text(text, encoding="utf-8")

    # --- 1. 権限・お願いのファイル -------------------------------------------------
    def test_画面を通さない要求では出せない_トークンとHostとよそのページ(self) -> None:
        from portal import web
        app = web.create_app(token="合言葉")
        client = app.test_client()
        with mock.patch("portal.admin_password.verify", return_value=True), \
                mock.patch.object(stop_mode, "administrator", return_value=(True, "")), \
                mock.patch.object(stop_mode, "configure"):
            self.assertEqual(client.post("/api/remote-stop/start", json={"password": "x"}).status_code, 401)
            self.assertEqual(client.post("/api/remote-stop/start", json={"password": "x"},
                                         headers={"X-Tool-Token": "合言葉", "Host": "example.com"}).status_code, 400)
            self.assertEqual(client.post("/api/remote-stop/start", json={"password": "x"},
                                         headers={"X-Tool-Token": "合言葉", "Sec-Fetch-Site": "cross-site"}).status_code,
                             403)
            self.assertEqual(client.post("/api/remote-stop/clear", json={}).status_code, 401)
        self.assertFalse(remote_stop.request_path().exists())

    def test_画面からでも_パスワードだけ_Administratorだけでは出せない(self) -> None:
        from portal import web
        client = web.create_app(token="合言葉").test_client()
        headers = {"X-Tool-Token": "合言葉"}
        with mock.patch.object(stop_mode, "configure"), mock.patch.object(stop_mode, "administrator_cached",
                                                                          return_value=(False, "")):
            with mock.patch("portal.admin_password.verify", return_value=True), \
                    mock.patch.object(stop_mode, "administrator", return_value=(False, "無い")):
                res = client.post("/api/remote-stop/start", json={"password": "x"}, headers=headers)
                self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (403, remote_stop.REFUSE_NOT_ADMIN))
            with mock.patch("portal.admin_password.verify", return_value=False), \
                    mock.patch.object(stop_mode, "administrator", return_value=(True, "")):
                res = client.post("/api/remote-stop/start", json={"password": "x"}, headers=headers)
                self.assertEqual((res.status_code, res.get_json()["error"]["code"]), (403, remote_stop.REFUSE_PASSWORD))
            with mock.patch("portal.admin_password.verify", return_value=True), \
                    mock.patch.object(stop_mode, "administrator", return_value=(True, "")):
                res = client.post("/api/remote-stop/start", json={"password": "", "reason": "x"}, headers=headers)
                self.assertEqual(res.status_code, 403, "空のパスワードは照合しない")
        self.assertFalse(remote_stop.request_path().exists())

    def test_手で書いた最小のお願いでも効き_同じ中身なら同じお願いと読む(self) -> None:
        self.write_request({"active": True})
        first = remote_stop.read_request()
        self.assertTrue(first["id"].startswith("manual-"))
        self.assertEqual(remote_stop.read_request()["id"], first["id"], "読むたびに「出し直した」と取り違えない")
        self.assertEqual(first["grace_sec"], 300)
        with self.as_pc("PC-LINE1"):
            self.assertIn("起動できません", remote_stop.startup_block())

    def test_おかしな値でも判定の途中で落ちて起動してしまわない(self) -> None:
        self.write_request({"active": True, "id": "x", "grace_sec": "abc", "created_at": "きのう", "by_pc": None})
        req = remote_stop.read_request()
        self.assertEqual((req["grace_sec"], req["created_at"], req["by_pc"]), (300, 0.0, ""))
        with self.as_pc("PC-LINE1"):
            self.assertIsNotNone(remote_stop.startup_block())
        for grace in (-5, 0, 10 ** 9, float("inf")):
            self.write_request({"active": True, "grace_sec": grace})
            self.assertEqual(remote_stop.read_request()["grace_sec"], 300, grace)

    def test_手で書いた出ていない印は出ていないと読む(self) -> None:
        for value in (False, "false", 0, "no", None, "いいえ"):
            self.write_request({"active": value, "id": "x"})
            self.assertIsNone(remote_stop.read_request(), value)
        for value in (True, "true", 1, "はい"):
            self.write_request({"active": value, "id": "x"})
            self.assertIsNotNone(remote_stop.read_request(), value)

    def test_壊れたJSONは読めないと同じ(self) -> None:
        """「お願い無し」とは読まない。覚えが無ければ起動はする(共有の瞬断で全 PC が起動できない、にしない)。"""
        self.write_request("{壊れ")
        with self.assertRaises(remote_stop.Unreadable):
            remote_stop.read_request()
        self.write_request("[1, 2]")
        with self.assertRaises(remote_stop.Unreadable):
            remote_stop.read_request()

    # --- 2. 止める範囲 -------------------------------------------------------------
    def test_お願いを出したPCの名前は大文字小文字と全角半角を問わない(self) -> None:
        self.assertTrue(remote_stop.same_pc("PC-ADMIN", " pc-admin "))
        self.assertTrue(remote_stop.same_pc("ＰＣ－ＡＤＭＩＮ", "PC-ADMIN"))
        self.assertFalse(remote_stop.same_pc("", ""))
        self.assertFalse(remote_stop.same_pc("PC-ADMIN2", "PC-ADMIN"))
        self.issue()
        with self.as_pc("pc-admin"):
            self.assertIsNone(remote_stop.startup_block())

    def test_お願いを出したPCが落ちても_ほかのAdministratorのPCで解除できる(self) -> None:
        self.issue()
        with self.as_pc("PC-ADMIN-2"):
            self.assertTrue(remote_stop.clear(True, True).ok)
        self.assertIsNone(remote_stop.read_request())

    # --- 4. 止める時 ---------------------------------------------------------------
    def test_数える処理が固まっても期限で強制になる(self) -> None:
        self.issue()
        now = [1000.0]
        stopped = []
        hang = threading.Event()
        self.addCleanup(hang.set)
        with self.as_pc("PC-LINE1"), mock.patch.object(remote_stop, "CALL_LIMIT_SEC", 0.2):
            w = remote_stop.Watcher(lambda: stopped.append(1), lambda: hang.wait(30) or [], flush=False,
                                    before_force=lambda: hang.wait(30) or [], clock=lambda: now[0])
            w.tick()
            self.assertEqual(w.left_now, ["残っているものを確かめられません"])
            now[0] += remote_stop.GRACE_SEC
            w.tick()
        self.assertEqual(stopped, [1])
        self.assertEqual(w.state, remote_stop.STATE_FORCED)
        self.assertTrue(list(remote_stop.keep_dir().glob("keep-*.json")))

    def test_控えを手元に書けなければ強制では止めず理由を出す(self) -> None:
        self.issue()
        now = [1000.0]
        stopped = []
        with self.as_pc("PC-LINE1"):
            w = remote_stop.Watcher(lambda: stopped.append(1), lambda: ["打ちかけ"], flush=False,
                                    before_force=lambda: ["x"], clock=lambda: now[0])
            w.tick()
            now[0] += remote_stop.GRACE_SEC + 1
            with mock.patch.object(remote_stop, "write_json", return_value=False):
                w.tick()
                w.tick()
            self.assertEqual(stopped, [], "控えを書けないのに強制で止めた")
            self.assertEqual(w.state, remote_stop.STATE_WAITING)
            self.assertIn("手元に控えを書けない", w.detail)
            self.assertIn(remote_stop.Watcher.KEEP_FAILED, w.screen_state()["left"])
            w.tick()                                       # 書けるようになった
        self.assertEqual(stopped, [1])

    def test_止める処理が返らなくても確実に落とす(self) -> None:
        self.issue()
        now = [1000.0]
        with self.as_pc("PC-LINE1"), mock.patch.object(remote_stop, "HARD_EXIT_SEC", 0.1), \
                mock.patch("os._exit") as exit_:
            w = remote_stop.Watcher(lambda: None, lambda: [], flush=False, hard_exit=True, clock=lambda: now[0])
            w.tick()
            now[0] += remote_stop.NOTICE_SEC
            w.tick()
            deadline = time.monotonic() + 5
            while not exit_.called and time.monotonic() < deadline:
                time.sleep(0.05)
        exit_.assert_called_once_with(0)
        self.assertEqual(self.answer_state(w), remote_stop.STATE_STOPPED)

    def answer_state(self, w) -> str:
        return json.loads(w.presence_path().read_text(encoding="utf-8"))["state"]

    # --- 5. 時計 -------------------------------------------------------------------
    def test_期限は各PCのmonotonicで数え_知らせの時刻は壁の時計(self) -> None:
        w = remote_stop.Watcher(lambda: None, lambda: [], flush=False)
        self.assertIs(w._clock, time.monotonic)
        self.issue()
        with self.as_pc("PC-LINE1"):
            w = remote_stop.Watcher(lambda: None, lambda: [], flush=False, clock=lambda: 5.0)
            w.tick()
        self.assertGreater(json.loads(w.presence_path().read_text(encoding="utf-8"))["beat_at"], 1e9)

    def test_最後に見たお願いは時計を大きく直したら使わない(self) -> None:
        self.issue()
        with self.as_pc("PC-LINE1"):
            remote_stop.startup_block()
            self.assertIsNotNone(remote_stop.remembered())
            data = json.loads(remote_stop.memory_path().read_text(encoding="utf-8"))
            data["seen_at"] = time.time() + remote_stop.MEMORY_KEEP_SEC * 2       # 先の時刻(時計を戻した)
            remote_stop.memory_path().write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(remote_stop.remembered())


class DefaultRootTests(unittest.TestCase):
    """単体の4ツールは、この PC の大設定 → 一式の配布設定 → 環境変数 → 既定 の順に置き場所を決める。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.settings = self.tmp / "AllTools" / "data" / "user_settings.json"
        env = {"ALLTOOLS_SETTINGS_PATH": str(self.settings), "ALLTOOLS_SHARED_DB_DIR": "",
               "ALLTOOLS_REMOTE_STOP_DIR": "", "ALLTOOLS_ROOT": str(self.tmp / "一式")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))
        remote_stop.configure(bundle_root=None)

    def test_決め方の順(self) -> None:
        self.assertEqual(str(remote_stop.default_root()), remote_stop.DEFAULT_SHARED_DIR)
        os.environ["ALLTOOLS_SHARED_DB_DIR"] = str(self.tmp / "環境変数")
        self.assertEqual(remote_stop.default_root(), self.tmp / "環境変数")
        dist = self.tmp / "一式" / "配布設定"
        dist.mkdir(parents=True)
        (dist / "日報複合ツール.json").write_text(json.dumps({"format": 1, "settings": {
            "shared_db_dir": str(self.tmp / "配布")}}), encoding="utf-8")
        self.assertEqual(remote_stop.default_root(), self.tmp / "配布")
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"shared_db_dir": str(self.tmp / "大設定")}), encoding="utf-8")
        self.assertEqual(remote_stop.default_root(), self.tmp / "大設定")
        os.environ["ALLTOOLS_REMOTE_STOP_DIR"] = str(self.tmp / "直に")
        self.assertEqual(remote_stop.default_root(), self.tmp / "直に")

    def test_大設定のファイルが読めなければ読めないとする(self) -> None:
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{壊れ", encoding="utf-8")
        with self.assertRaises(remote_stop.Unreadable):
            remote_stop.default_root()

    def test_入口と同じ置き場所になる(self) -> None:
        """入口は大設定の `shared_db.location`、4ツールは `default_root`。同じ設定なら同じフォルダ。"""
        from portal import shared_db
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"shared_db_dir": str(self.tmp / "大設定")}), encoding="utf-8")
        self.assertEqual(Path(shared_db.location(strict=True).folder), remote_stop.default_root())


# ======================================================================
# 本物の子プロセス(ブラウザ版の入口)
# ======================================================================
TIMES = "grace=4,notice=0.5,poll=0.2,flush=0.5,host_extra=0.5,orphan=3,beat=1,local_stale=5"


class RealPortalTests(unittest.TestCase):
    """`start_app.py --no-browser` を本物の子プロセスで起こし、お願いで止まるのを見る。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.share = self.tmp / "共有"
        self.share.mkdir()
        self.left_file = self.tmp / "残っているもの.txt"
        self.env = dict(os.environ)
        self.env.update({
            "ALLTOOLS_LOCAL_DIR": str(self.tmp / "AllTools"),
            "ALLTOOLS_SETTINGS_PATH": str(self.tmp / "AllTools" / "data" / "user_settings.json"),
            "ALLTOOLS_SHARED_DB_DIR": str(self.share),
            "ALLTOOLS_PC_NAME": "PC-LINE1", "ALLTOOLS_LOGIN_ID": "genba",
            "ALLTOOLS_REMOTE_STOP_TIMES": TIMES,
            "ALLTOOLS_REMOTE_STOP_TEST_LEFT": str(self.left_file),
            "ALLTOOLS_LOG_QUIET": "1", "PYTHONIOENCODING": "utf-8",
        })
        self.env.pop("ALLTOOLS_REMOTE_STOP_DIR", None)
        self.procs: list = []
        self.addCleanup(self._kill)
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))
        remote_stop.configure(root=lambda: self.share, local_root=self.tmp / "管理者の手元", logger=stop_mode.log)

    def _kill(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    def start(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, str(ROOT / "start_app.py"), "--no-browser"], cwd=str(ROOT),
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def wait_listening(self, proc) -> None:
        lock = self.tmp / "AllTools" / "runtime" / "portal.lock"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"起動しませんでした: {proc.stderr.read().decode('utf-8', 'replace')}")
            if lock.exists():
                time.sleep(0.5)
                return
            time.sleep(0.1)
        self.fail("起動しませんでした(印が出ない)")

    def issue_as_admin(self) -> None:
        # 待つ長さはお願いに書く(`grace_sec`)。試験は短く
        with mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": "PC-ADMIN", "ALLTOOLS_LOGIN_ID": "kanri"}), \
                mock.patch.object(remote_stop, "GRACE_SEC", 4.0):
            self.assertTrue(remote_stop.issue(True, True, "更新のため").ok)

    def clear_as_admin(self) -> None:
        with mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": "PC-ADMIN"}):
            self.assertTrue(remote_stop.clear(True, True).ok)

    def answers(self) -> dict:
        out = {}
        for path in (self.share / "管理者以外停止" / "稼働中").glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            out[data["pc"]] = data
        return out

    def test_お願いで止まる_お願い中は起動しない_解除で起動できる(self) -> None:
        proc = self.start()
        self.wait_listening(proc)
        self.issue_as_admin()
        self.assertEqual(proc.wait(30), 0, "お願いで止まらない")
        answer = self.answers()["PC-LINE1"]
        self.assertEqual(answer["state"], remote_stop.STATE_STOPPED)
        self.assertEqual(answer["app"], "日報複合ツール")

        blocked = self.start()
        self.assertEqual(blocked.wait(30), 1, "お願い中に起動した")
        self.assertIn("管理者が更新のため", blocked.stderr.read().decode("utf-8", "replace"))
        self.assertFalse((self.tmp / "AllTools" / "runtime" / "portal.lock").exists())

        self.clear_as_admin()
        again = self.start()
        self.wait_listening(again)
        self.assertIsNone(again.poll(), "解除したのに起動しない")

    def test_残っているものがあれば待ち_期限で強制で止まる(self) -> None:
        self.left_file.write_text("試験の打ちかけ\n", encoding="utf-8")
        proc = self.start()
        self.wait_listening(proc)
        started = time.monotonic()
        self.issue_as_admin()
        time.sleep(2)
        self.assertIsNone(proc.poll(), "残っているのに止まった")
        self.assertEqual(self.answers()["PC-LINE1"]["state"], remote_stop.STATE_WAITING)
        self.assertEqual(proc.wait(30), 0)
        self.assertGreaterEqual(time.monotonic() - started, 4, "期限より前に止まった")
        answer = self.answers()["PC-LINE1"]
        self.assertEqual(answer["state"], remote_stop.STATE_FORCED)
        self.assertIn("試験の打ちかけ", answer["detail"])

    def test_残っているものが片付けば期限を待たずに止まる(self) -> None:
        self.left_file.write_text("試験の打ちかけ\n", encoding="utf-8")
        proc = self.start()
        self.wait_listening(proc)
        self.issue_as_admin()
        time.sleep(1.5)
        self.assertIsNone(proc.poll())
        self.left_file.unlink()
        self.assertEqual(proc.wait(30), 0)
        self.assertEqual(self.answers()["PC-LINE1"]["state"], remote_stop.STATE_STOPPED)

    def test_取り下げたら止まらない(self) -> None:
        self.left_file.write_text("試験の打ちかけ\n", encoding="utf-8")
        proc = self.start()
        self.wait_listening(proc)
        self.issue_as_admin()
        time.sleep(1.5)
        self.clear_as_admin()
        time.sleep(5)
        self.assertIsNone(proc.poll(), "取り下げたのに止まった")

    def test_中のツールもまとめて止まる(self) -> None:
        """日報複合ツールから起こしたツール(ここでは点検表のブラウザ版)は自分では止まらず、手元に答える。
        入口は、中のツールが止めてよいと答えるのを待ってから、まとめて閉じる。"""
        import http.client
        self.env["INSPECTION_LOCAL_DIR"] = str(self.tmp / "Inspection")
        self.left_file.write_text("試験の打ちかけ\n", encoding="utf-8")
        proc = self.start()
        self.wait_listening(proc)
        lock = json.loads((self.tmp / "AllTools" / "runtime" / "portal.lock").read_text(encoding="utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", lock["port"], timeout=120)
        conn.request("POST", "/api/tools/inspection/open", body="{}",
                     headers={"X-Tool-Token": lock["token"], "Content-Type": "application/json"})
        res = conn.getresponse()
        self.assertEqual(res.status, 200, res.read())
        conn.close()
        tool_lock = next((self.tmp / "Inspection" / "runtime").glob("*.lock"))
        tool_pid = json.loads(tool_lock.read_text(encoding="utf-8"))["pid"]
        self.issue_as_admin()
        time.sleep(2)
        local = list((self.tmp / "AllTools" / "runtime" / "管理者以外停止").glob("inspection-*.json"))
        self.assertEqual(len(local), 1, "中のツールが入口へ答えていない")
        self.assertIn("試験の打ちかけ", json.loads(local[0].read_text(encoding="utf-8"))["left"])
        self.assertIsNone(proc.poll(), "残っているのに止まった")
        self.assertEqual(proc.wait(40), 0)
        from portal import browser_tools
        deadline = time.monotonic() + 20
        while browser_tools.is_alive(tool_pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(browser_tools.is_alive(tool_pid), "中のツールが残っている")
        answers = {(a["pc"], a["app_id"]): a for a in (json.loads(p.read_text(encoding="utf-8")) for p in
                   (self.share / "管理者以外停止" / "稼働中").glob("*.json"))}
        self.assertEqual(answers[("PC-LINE1", "portal")]["state"], remote_stop.STATE_FORCED)
        self.assertEqual(answers[("PC-LINE1", "inspection")]["state"], remote_stop.STATE_FORCED)
        self.assertTrue(answers[("PC-LINE1", "inspection")]["hosted"])
        self.assertTrue(list((self.tmp / "Inspection" / "data" / "停止控え").glob("keep-*.json")),
                        "中のツールも強制の前に控えを書く")

    def test_デスクトップ版の入口もお願い中は起動せず_外枠へ理由を渡す(self) -> None:
        """`bridge.py`(日報複合ツール.exe が起こす入口)は fatal で理由を渡す(外枠が窓に出す)。"""
        self.issue_as_admin()
        proc = subprocess.Popen([sys.executable, str(ROOT / "bridge.py")], cwd=str(ROOT), env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        out, _err = proc.communicate(timeout=60)
        proc.stdin = None
        events = [json.loads(line) for line in out.decode("utf-8").splitlines() if line.startswith("{")]
        self.assertEqual(events[0]["event"], "fatal", events)
        self.assertIn("管理者が更新のため", events[0]["message"])
        self.assertIn("別の名前に変えると解除できます", events[0]["hint"])
        self.assertEqual(proc.returncode, 1)

    def test_起動の途中にお願いが出ても止まる(self) -> None:
        """起動の入口の判定のすぐあと(見張りが立つ前)にお願いが出ても、見張りが立てば止まる。"""
        proc = self.start()
        deadline = time.monotonic() + 30
        while not (self.tmp / "AllTools" / "runtime" / "portal.lock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.issue_as_admin()
        self.assertEqual(proc.wait(40), 0)

    def test_共有に届かなくても起動はする(self) -> None:
        """共有が落ちているだけで、全部の PC が起動できなくなる、にしない(この PC が最後に見たお願いも無い)。"""
        # 届かない共有のまね: 親がファイル(一覧できない)。「無い」とは確かめられない
        (self.tmp / "ファイル").write_text("x", encoding="utf-8")
        self.env["ALLTOOLS_REMOTE_STOP_DIR"] = str(self.tmp / "ファイル" / "共有")
        proc = self.start()
        self.wait_listening(proc)
        self.assertIsNone(proc.poll())

    def test_共有を読めないとき_最後に見たお願いが出ていれば起動しない(self) -> None:
        proc = self.start()
        self.wait_listening(proc)
        self.issue_as_admin()
        self.assertEqual(proc.wait(30), 0)
        (self.share / "管理者以外停止" / "お願い.json").write_text("{書いている途中", encoding="utf-8")
        blocked = self.start()
        self.assertEqual(blocked.wait(30), 1)
        self.assertIn("解除されたかを確かめられません", blocked.stderr.read().decode("utf-8", "replace"))


# ======================================================================
# 画面の帯(本物のブラウザ。ALLTOOLS_REQUIRE_BROWSER=1 なら飛ばさない)
# ======================================================================
def _browser_case():
    from tests._shell_browser import ShellBrowserCase
    return ShellBrowserCase


class BandBrowserTests(_browser_case()):
    """大きなタブの画面の上に帯を出し、止めると決まったら各ツールの画面に打ちかけを置いてもらう。"""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.share = Path(tmp.name) / "共有"
        self.share.mkdir()
        saved = remote_stop.Setup(**vars(remote_stop.setup()))
        self.addCleanup(lambda: remote_stop.configure(**vars(saved)))
        remote_stop.configure(root=lambda: self.share, local_root=Path(tmp.name) / "手元", logger=stop_mode.log)
        remote_stop.reset()
        self.addCleanup(remote_stop.reset)
        self.now = [1000.0]
        self.closed = []
        with mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": "PC-ADMIN"}):
            remote_stop.issue(True, True, "版の入れ替え")
        self.watcher = remote_stop.Watcher(lambda: self.closed.append(1), lambda: ["看板: 送っていない操作 2 件"],
                                           extra_sec=0, clock=lambda: self.now[0])
        remote_stop._watcher = self.watcher

    def test_帯に残りと残っているものを出し_止める前に各ツールの画面に打ちかけを置いてもらう(self) -> None:
        self.watcher.tick()
        page = self.open_shell(first="nippou")
        page.wait_for_selector("#stopband:not([hidden])", timeout=10000)
        text = page.inner_text("#stopband")
        self.assertIn("管理者が更新のため停止を求めています", text)
        self.assertIn("版の入れ替え", text)
        self.assertIn("看板: 送っていない操作 2 件", text)
        self.assertIn("強制で止めます", text)
        # タブを替えても帯は消えない(全ツールの画面の上)
        page.click("#tab-settings")
        self.assertTrue(page.is_visible("#stopband"))

        # 期限: 止めると決まる → 各ツールの画面に「閉じる前」の頼み → 入口へ「置きました」
        self.now[0] += remote_stop.GRACE_SEC + 1
        self.watcher._left_fn = lambda: []
        self.watcher.tick()
        self.assertTrue(self.watcher.screen_state()["stopping"])
        page.wait_for_function("document.getElementById('stopband-line').textContent.includes('閉じます')",
                               timeout=10000)
        self.wait_until(page, lambda: self.watcher.stop_at <= self.now[0], "画面が「置きました」を返す", timeout=15)
        self.assertTrue(self.kinds("tool", type="alltools:before-close"), "ツールの画面に打ちかけを置く頼みが届かない")

    def test_取り下げたら帯が消え_一度だけ知らせる(self) -> None:
        self.watcher.tick()
        page = self.open_shell(first="nippou")
        page.wait_for_selector("#stopband:not([hidden])", timeout=10000)
        with mock.patch.dict(os.environ, {"ALLTOOLS_PC_NAME": "PC-ADMIN"}):
            remote_stop.clear(True, True)
        self.watcher.tick()
        page.wait_for_selector("#stopband", state="hidden", timeout=10000)
        page.wait_for_function("document.getElementById('toasts').textContent.includes('取り下げました')",
                               timeout=10000)


if __name__ == "__main__":
    unittest.main()
