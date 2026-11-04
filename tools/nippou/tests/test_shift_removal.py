"""直を消す ── 手元で要らないページを探して消す・共有からも消すかは選ぶ

    そのPCの手元で不要なものを探す → 消す → 選択制「共有の日報データからも消しますか？」
    yes で共有からも消す

確かめること:

    - 探す: テストデータ / 中身の無いページ / 日付・ライン・直で指定 の3つの一覧
    - 消すのは管理者モードだけ。消す前に控え(%LOCALAPPDATA% の下)を残す。残せなければ消さない
    - 一覧を出してから押すまでのあいだに打たれたページは消さない
    - 手元だけ(いいえ)/ 共有からも(はい ── そのページの行だけ、1つの取引で)
    - 共有に届かない・読めないときは共有を消さず、「共有はまだ」と残して後で消せる
    - 手元にまた保存されたページは共有から消さない
    - 消したら集計も作り直す(ページが残らない直は集計も消す)
    - 誰が(PC名・ログインID)いつ何を消したかが記録に残る
    - 控え(LocalBackup): 消したページを起動のときに生き返らせない・同じラインのほかの PC も消す
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

DAY = "2026年8月3日"
OTHER_DAY = "2026年8月4日"


def key_of(shift: str = "1直", page: int = 1, day: str = DAY, line: str = "L-1") -> dict:
    return dict(report_date=day, line=line, shift=shift, page=page)


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class RemovalCase(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        from nippou.services import local_backup

        self.share = self.tmp / "共有"
        self.share.mkdir()
        user_settings.save_many({config.KEY_ACCESS_DIR: str(self.share)})
        local_backup.reset()
        self.addCleanup(local_backup.reset)

    @property
    def shared(self) -> Path:
        return self.share / "日報データ.sqlite3"

    def password(self) -> str:
        from nippou.config import SETTINGS

        return SETTINGS.admin_password

    def admin(self) -> None:
        res = self.post("/api/settings/admin", {"enable": True, "password": self.password()})
        self.assertEqual(res.status_code, 200)

    def save(self, key: dict, n: int = 2, *, lot: str = "A", worker: str = "山田") -> None:
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.services import summary

        details = [DetailRecord(**key, row_no=i, lot=f"{lot}{i}", ken="10", con="10",
                                wei="12.5", kz="8", kh="00", sz="9", sh="00")
                   for i in range(1, n + 1)]
        repo = self.repo()
        repo.save(HeaderRecord(**key, worker=worker), details)
        summary.refresh_shift(repo, key["report_date"], key["line"], key["shift"])

    def save_empty(self, key: dict) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord

        self.repo().save(HeaderRecord(**key, worker="山田"),
                         [DetailRecord(**key, row_no=i) for i in range(1, 13)])

    def switch_test_mode(self, on: bool) -> None:
        res = self.post("/api/test-mode", {"enable": on, "password": self.password()})
        self.assertEqual(res.status_code, 200)

    def push(self) -> None:
        from nippou.access_bridge import pusher

        summary = pusher.push_pending(self.repo(), self.shared)
        self.assertEqual(summary.failed, [])

    def listed(self, kind: str = "all", **query) -> list[dict]:
        args = "&".join(f"{k}={v}" for k, v in {"kind": kind, **query}.items())
        res = self.get(f"/api/settings/removal?{args}")
        self.assertEqual(res.status_code, 200)
        return res.get_json()["pages"]

    def item(self, page: dict) -> dict:
        return {k: page[k] for k in ("report_date", "line", "shift", "page", "stamp")}

    def delete(self, pages: list[dict]):
        return self.post("/api/settings/removal/delete", {"items": [self.item(p) for p in pages]})

    def keys(self) -> set[tuple]:
        return set(self.repo().list_keys())

    def shared_rows(self, table: str = "T_日報ヘッダー_L-1") -> list[tuple]:
        conn = sqlite3.connect(str(self.shared))
        try:
            return sorted(conn.execute(f'SELECT "報告日", "直", "頁" FROM "{table}"').fetchall())
        finally:
            conn.close()

    def shared_count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.shared))
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()


class FindTests(RemovalCase):
    def setUp(self) -> None:
        super().setUp()
        self.save(key_of("1直"))
        self.save_empty(key_of("1直", page=2))
        self.switch_test_mode(True)
        self.save(key_of("2直"), n=3)
        self.save(key_of("2直", page=2), n=1)
        self.switch_test_mode(False)
        self.save(key_of("1直", day=OTHER_DAY, line="LVC"))

    def test_テストデータの一覧(self) -> None:
        got = self.listed("test")
        self.assertEqual([(p["shift"], p["page"]) for p in got], [("2直", 2), ("2直", 1)])
        self.assertTrue(all(p["test"] for p in got))
        self.assertEqual([p["rows"] for p in got], [1, 3])

    def test_中身の無いページの一覧は片付けと同じ物差し(self) -> None:
        got = self.listed("empty")
        self.assertEqual([(p["shift"], p["page"], p["rows"]) for p in got], [("1直", 2, 0)])
        self.assertTrue(got[0]["empty"])

    def test_日付とラインと直で指定して選ぶ(self) -> None:
        self.assertEqual(len(self.listed("all")), 5)
        got = self.listed("all", day="2026-08-04")
        self.assertEqual([(p["line"], p["shift"]) for p in got], [("LVC", "1直")])
        got = self.listed("all", line="L-1", shift="1直")
        self.assertEqual({p["page"] for p in got}, {1, 2})
        body = self.get("/api/settings/removal?kind=all").get_json()
        self.assertEqual({c["code"] for c in body["choices"]["lines"]}, {"L-1", "LVC"})

    def test_共有へ送ったかが見える(self) -> None:
        self.push()
        self.save(key_of("1直"), lot="B")                        # 送ったあとに直した
        got = {(p["shift"], p["page"]): p for p in self.listed("all", line="L-1")}
        self.assertTrue(got[("1直", 1)]["sent"])
        self.assertTrue(got[("1直", 1)]["unsent"])
        self.assertTrue(got[("2直", 1)]["sent"])
        self.assertFalse(got[("2直", 1)]["unsent"])

    def test_見るだけなら管理者でなくてよいが消すのは管理者だけ(self) -> None:
        got = self.listed("test")
        res = self.delete(got)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(len(self.keys()), 5)


class RemoveTests(RemovalCase):
    def setUp(self) -> None:
        super().setUp()
        self.save(key_of("1直"))
        self.switch_test_mode(True)
        self.save(key_of("2直"), n=3)
        self.save(key_of("2直", page=2), n=1)
        self.switch_test_mode(False)
        self.push()
        self.admin()

    def test_手元だけ消す(self) -> None:
        from nippou.services import shift_removal

        res = self.delete(self.listed("test"))
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertTrue(body["ask_shared"])
        self.assertEqual(len(body["ids"]), 2)
        self.assertEqual(self.keys(), {(DAY, "L-1", "1直", 1)})
        # 消す前の控え(%LOCALAPPDATA% の下)に、消したページが入っている
        kept = Path(body["kept_file"])
        self.assertTrue(str(kept).startswith(str(shift_removal.kept_dir())))
        conn = sqlite3.connect(str(kept))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_header").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_detail").fetchone()[0], 4)
        finally:
            conn.close()
        # いいえ → 手元だけ。共有の行は残る
        res = self.post("/api/settings/removal/shared", {"ids": body["ids"], "remove": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.shared_rows()), 3)
        states = {r["shared"] for r in self.repo().removals()}
        self.assertEqual(states, {"local"})
        self.assertEqual(res.get_json()["open"], [])

    def test_共有からも消す_そのページの行だけ(self) -> None:
        res = self.delete([p for p in self.listed("test") if p["page"] == 2])
        ids = res.get_json()["ids"]
        res = self.post("/api/settings/removal/shared", {"ids": ids, "remove": True})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.shared_rows(), [(DAY, "1直", 1), (DAY, "2直", 1)])
        self.assertEqual(self.shared_count("T_日報明細_L-1"), 2 + 3)
        self.assertEqual({r["shared"] for r in self.repo().removals()}, {"done"})

    def test_共有からも消す_直のページが残らなければ集計の行も(self) -> None:
        self.save(key_of("3直"))                                  # 本番の直を足して送る
        self.push()
        before = self.shared_count("T_日報集計_L-1")
        res = self.delete(self.listed("all", shift="3直"))
        self.assertIsNone(self.repo().load_packing_report(DAY, "L-1", "3直"), "手元の集計が残っています")
        res = self.post("/api/settings/removal/shared", {"ids": res.get_json()["ids"], "remove": True})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.shared_count("T_日報集計_L-1"), before - 1)
        self.assertEqual(self.shared_rows(), [(DAY, "1直", 1), (DAY, "2直", 1), (DAY, "2直", 2)])

    def test_共有に届かなければ消さずに残し後で消せる(self) -> None:
        from nippou.db import connection

        res = self.delete(self.listed("test"))
        ids = res.get_json()["ids"]

        def unreadable(path, *a, **k):
            raise connection.SharedUnreadable(f"届きません: {path}")

        with patch.object(connection, "file_state", side_effect=unreadable):
            res = self.post("/api/settings/removal/shared", {"ids": ids, "remove": True})
        self.assertEqual(res.status_code, 503)
        body = res.get_json()
        self.assertIn("消せませんでした", body["message"])
        self.assertIn("届かない", body["message"])
        self.assertEqual(len(body["open"]), 2, "「共有はまだ」が残っていません")
        self.assertEqual(len(self.shared_rows()), 3, "読めないのに消しています")
        self.assertEqual(self.keys(), {(DAY, "L-1", "1直", 1)}, "手元は消したまま")
        # 届くようになったら、残った一覧から消せる
        listed = self.get("/api/settings/removal?kind=test").get_json()["open"]
        res = self.post("/api/settings/removal/shared",
                        {"ids": [r["id"] for r in listed], "remove": True})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.shared_rows(), [(DAY, "1直", 1)])
        self.assertEqual(res.get_json()["open"], [])

    def test_共有のファイルが無ければ作らない(self) -> None:
        res = self.delete(self.listed("test"))
        self.shared.unlink()
        res = self.post("/api/settings/removal/shared",
                        {"ids": res.get_json()["ids"], "remove": True})
        self.assertEqual(res.status_code, 503)
        self.assertFalse(self.shared.exists(), "消すために共有の日報データを作っています")

    def test_押すまでに打たれたページは消さない(self) -> None:
        listed = self.listed("test")
        self.save(key_of("2直"), n=4, lot="Z")                    # 一覧を出したあとに打った
        res = self.delete(listed)
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(len(body["removed"]), 1)
        self.assertIn("一覧を出したあとに保存された", body["skipped"][0]["why"])
        self.assertIn((DAY, "L-1", "2直", 1), self.keys())
        self.assertNotIn((DAY, "L-1", "2直", 2), self.keys())

    def test_全部打たれていたら422(self) -> None:
        listed = [p for p in self.listed("test") if p["page"] == 1]
        self.save(key_of("2直"), n=4, lot="Z")
        res = self.delete(listed)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(len(self.keys()), 3)

    def test_控えを残せなければ消さない(self) -> None:
        from nippou.services import shift_removal

        with patch.object(shift_removal, "_keep", side_effect=OSError("書けません")):
            res = self.delete(self.listed("test"))
        self.assertEqual(res.status_code, 500)
        self.assertIn("控えを残せなかった", res.get_json()["error"]["message"])
        self.assertEqual(len(self.keys()), 3)

    def test_手元にまた保存されたら共有から消さない(self) -> None:
        res = self.delete([p for p in self.listed("test") if p["page"] == 2])
        ids = res.get_json()["ids"]
        self.save(key_of("2直", page=2), n=1, lot="Q")            # 消したあとに打ち直した
        res = self.post("/api/settings/removal/shared", {"ids": ids, "remove": True})
        self.assertEqual(res.status_code, 200)
        self.assertIn("手元にまた保存されています", res.get_json()["message"])
        self.assertEqual(len(self.shared_rows()), 3)
        self.assertEqual({r["shared"] for r in self.repo().removals()}, {"skipped"})

    def test_消したら集計を作り直す(self) -> None:
        from nippou.services import summary

        self.save(key_of("3直"), n=2)
        self.save(key_of("3直", page=2), n=3, lot="B")
        before = summary.day_rows(self.repo(), DAY, "L-1")
        self.assertEqual({r.shift: r.sheet_count for r in before}, {"1直": 20, "3直": 50})
        self.delete([p for p in self.listed("all", shift="3直") if p["page"] == 2])
        after = summary.day_rows(self.repo(), DAY, "L-1")
        self.assertEqual({r.shift: r.sheet_count for r in after}, {"1直": 20, "3直": 20})

    def test_誰がいつ何を消したかが記録に残る(self) -> None:
        from nippou.services import event_log

        res = self.delete(self.listed("test"))
        self.post("/api/settings/removal/shared", {"ids": res.get_json()["ids"], "remove": True})
        event_log.flush(5.0)
        found = {r["action"]: r for r in event_log.load(date.today(), date.today())
                 if str(r.get("action", "")).startswith("pages_removed")}
        self.assertIn("pages_removed", found)
        self.assertIn("pages_removed_shared", found)
        self.assertIn("ログインID", found["pages_removed"]["where"])
        self.assertIn(f"{DAY} L-1 2直 第1ページ", found["pages_removed"]["key"])
        record = self.repo().removals()[0]
        self.assertTrue(record["terminal"] or record["login"])
        self.assertTrue(record["removed_at"])
        self.assertEqual(record["reason"], "all")

    def test_ページの記録は消しても消えない(self) -> None:
        """消した記録(`page_removal`)は残る。中身のある・テストの・送ったページかも残す。"""
        self.delete(self.listed("test"))
        records = {(r["page"], r["rows"], r["test_mark"], r["was_sent"]) for r in self.repo().removals()}
        self.assertEqual(records, {(1, 3, 1, 1), (2, 1, 1, 1)})


class AccessTargetTests(RemovalCase):
    """共有が Access(.accdb)のとき。**全ページを1つの取引で**消す。開けなければ消さない。"""

    def setUp(self) -> None:
        super().setUp()
        self.save(key_of("1直"))
        self.save(key_of("1直", page=2))
        self.admin()
        res = self.delete(self.listed("all"))
        self.ids = res.get_json()["ids"]

    def run_shared(self, unread: str = ""):
        from nippou.access_bridge import pusher, script_gen
        from nippou.access_bridge.runner import ScriptResult
        from nippou.services import shift_removal

        calls: list[list[str]] = []

        def build(path, statements):
            calls.append(list(statements))
            return "' script"

        class Runner:
            def run(self, text):
                return ScriptResult(success=True, rows=1)

        with patch.object(pusher, "access_unread", return_value=unread), \
                patch.object(pusher, "access_table", return_value=pusher.VBA_TABLE), \
                patch.object(script_gen, "build_push_script", side_effect=build):
            out = shift_removal.remove_shared(self.repo(), self.ids,
                                              db_path=self.share / "日報データ.accdb",
                                              runner=Runner())
        return out, calls

    def test_1つの取引でそのページの行だけ消す(self) -> None:
        out, calls = self.run_shared()
        self.assertEqual(len(calls), 1, "ページごとに別の取引にしています")
        sql = "\n".join(calls[0])
        self.assertEqual(sql.count("DELETE FROM [T_日報ヘッダー_L-1]"), 2)
        self.assertEqual(sql.count("DELETE FROM [T_日報明細_L-1]"), 2)
        self.assertIn("[頁]=2", sql)
        self.assertEqual(len(out.done), 2)

    def test_開けなければ消さない(self) -> None:
        out, calls = self.run_shared(unread="ほかで掴んでいます")
        self.assertEqual(calls, [])
        self.assertEqual(len(out.pending), 2)
        self.assertEqual({r["shared"] for r in self.repo().removals()}, {"pending"})


class BackupTests(RemovalCase):
    """控え(LocalBackup): 消したページを**生き返らせない。**"""

    def setUp(self) -> None:
        super().setUp()
        from nippou.services import local_backup

        self.save(key_of("1直"))
        self.save(key_of("2直"))
        local_backup.flush(self.repo(), force=True)
        self.admin()

    def backup_keys(self) -> set[tuple]:
        from nippou.services import local_backup

        with local_backup.reader("L-1") as backup:
            return set(backup.list_keys())

    def test_消したページは控えからも消え記録が写る(self) -> None:
        from nippou.services import local_backup

        self.delete(self.listed("all", shift="2直"))
        local_backup.flush(self.repo(), force=True)
        self.assertEqual(self.backup_keys(), {(DAY, "L-1", "1直", 1)})
        with local_backup.reader("L-1") as backup:
            self.assertEqual(len(backup.removals()), 1)

    def test_控えへ写す前に起動しても生き返らない(self) -> None:
        from nippou.services import local_backup

        with patch.object(local_backup, "flush_soon"):        # 応答のあとに写さない
            self.delete(self.listed("all", shift="2直"))
        self.assertIn((DAY, "L-1", "2直", 1), self.backup_keys())
        local_backup.pull(self.repo(), "L-1")                  # 起動のとき
        self.assertNotIn((DAY, "L-1", "2直", 1), self.keys(), "消したページが戻っています")
        local_backup.flush(self.repo(), force=True)
        self.assertNotIn((DAY, "L-1", "2直", 1), self.backup_keys())

    def test_同じラインのほかのPCも同じ中身なら消す(self) -> None:
        """もう1台が同じページを持っていると、起動のときに控えへ戻していた。"""
        from nippou.db.connection import connect
        from nippou.db.repository import NippouRepository
        from nippou.services import local_backup

        other = NippouRepository(connect(self.tmp / "other-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        local_backup.pull(other, "L-1")                         # もう1台も同じ中身を持つ
        self.assertEqual(len(other.list_keys()), 2)
        self.delete(self.listed("all", shift="2直"))
        local_backup.flush(self.repo(), force=True)
        result = local_backup.pull(other, "L-1")                # もう1台の起動
        self.assertEqual(set(other.list_keys()), {(DAY, "L-1", "1直", 1)})
        self.assertEqual(result.removed, 1)
        local_backup.flush(other, force=True)
        self.assertEqual(self.backup_keys(), {(DAY, "L-1", "1直", 1)}, "控えへ戻しています")

    def test_ほかのPCで直した中身は消さない(self) -> None:
        from nippou.db.connection import connect
        from nippou.db.models import DetailRecord, HeaderRecord
        from nippou.db.repository import NippouRepository
        from nippou.services import local_backup

        other = NippouRepository(connect(self.tmp / "other-pc.sqlite3"))
        self.addCleanup(other.conn.close)
        local_backup.pull(other, "L-1")
        key = key_of("2直")
        other.save(HeaderRecord(**key, worker="田中"),
                   [DetailRecord(**key, row_no=1, lot="NEW")])     # もう1台で直した
        self.delete(self.listed("all", shift="2直"))
        local_backup.flush(self.repo(), force=True)
        local_backup.pull(other, "L-1")
        self.assertIn((DAY, "L-1", "2直", 1), set(other.list_keys()))


if __name__ == "__main__":
    unittest.main()
