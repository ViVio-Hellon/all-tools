"""通しの試験 ── 直代わり・保存の有無・エラーがあるとき・共有へまとめて送るとき

【利用者の疑問】

    直代わりのタイミング、保存の有無、エラーありの時の対処においてもう1回
    一通りテストをしてください
    エラーがたまったまま続けるとどうなるか、共有保存なしで進めるとどうなるか、
    たまった共有保存を一気に保存した場合に正しく行きわたるのか

1つの関数を試すのではなく、**利用者が画面でやる順に経路を叩いて**、
そのあとに残るもの(共有の表・手元の表・未送信の印・履歴・引き継ぎ・集計・
画面の応答)をまとめて確かめます。

【時計は止める】
直の境を試すのに本物の時計は使いません。アプリの各モジュールが
`from datetime import datetime, date` で持っている名前を、決めた時刻を返す
ものに差し替えます(`FrozenDateTime` / `FrozenDate`)。`tests/test_sound_events.py` が1つの
モジュールでやっている差し替えを、アプリ全体に広げたものです。

【直の時刻】
現場と同じ形を時間マスタに置きます。3直は日をまたぎます。

    1直 07:00〜15:00 / 2直 15:00〜22:50 / 3直 22:50〜07:00 / 日勤 08:00〜17:00

【共有】
一時フォルダの `日報データ.sqlite3`。送るのは画面と同じ `POST /api/settings/push`。

【共有へ保存は2つ(v4.35.0)】

    現在の直と前直は処理の違うボタンを設けたほうがいいと思うのですが...
    ・普段の共有へ送る は現在の直用
    ・エラー時に出ている 共有へ送る は 前直処理用

    push_now()   … 入力画面のふだんの「共有へ保存」(`scope: "current"`)。いまの直だけ
    push_past()  … 赤い帯の「過去分を共有へ送る」(`scope: "previous"`)。いまの直では
                   ない未送信の直ぜんぶを古い順に。エラーのある直だけ残して、ほかは送る
    push()       … 範囲を言わない(まとめて送る。前の版の画面・ほかの道具。E の Access の試験)
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import sqlite3
import sys
import unittest
from datetime import date as _real_date
from datetime import datetime as _real_datetime
from datetime import timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402

SHIFT_TIMES = {"1": ("07:00", "15:00"), "2": ("15:00", "22:50"),
               "3": ("22:50", "07:00"), "昼": ("08:00", "17:00")}
SKIP = "設備移動のため保存"


# ======================================================================
# 止めた時計
# ======================================================================
class _Now:
    """いま。テストが `at()` で動かす。"""

    value: _real_datetime = _real_datetime(2026, 10, 5, 8, 0)


class _DateTimeMeta(type):
    def __instancecheck__(cls, obj):             # 本物の datetime も「そう」と答える
        return isinstance(obj, _real_datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_datetime)


class _DateMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_date)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_date)


class FrozenDateTime(_real_datetime, metaclass=_DateTimeMeta):
    @classmethod
    def now(cls, tz=None):                        # noqa: D401 - datetime と同じ形
        value = _Now.value
        return value if tz is None else value.replace(tzinfo=tz)

    @classmethod
    def today(cls):
        return _Now.value


class FrozenDate(_real_date, metaclass=_DateMeta):
    @classmethod
    def today(cls):
        return _Now.value.date()


def _load_all_modules() -> None:
    """アプリの全モジュールを先に読み込む(あとから読まれたものは差し替わらない)。"""
    for name in ("nippou", "app"):
        package = importlib.import_module(name)
        for info in pkgutil.walk_packages(package.__path__, name + "."):
            try:
                importlib.import_module(info.name)
            except Exception:                    # noqa: BLE001 - 読めないものは時計を使わない
                pass


def install_clock(case: unittest.TestCase) -> None:
    """アプリの `datetime` `date` を、止めた時計に差し替える(片付けはテストの終わり)。"""
    _load_all_modules()
    swapped = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name in ("nippou", "app")
                                  or name.startswith(("nippou.", "app."))):
            continue
        for attr, real, fake in (("datetime", _real_datetime, FrozenDateTime),
                                 ("date", _real_date, FrozenDate)):
            if getattr(module, attr, None) is real:
                setattr(module, attr, fake)
                swapped.append((module, attr, real))

    def restore():
        for module, attr, real in swapped:
            setattr(module, attr, real)
    case.addCleanup(restore)


# ======================================================================
# 打つ中身
# ======================================================================
def row(lot: str, start: str, end: str, **extra) -> dict:
    """1行。`start`/`end` は "07:00" の形。"""
    values = {"LOT": lot, "KZ": start[:2], "KH": start[3:], "SZ": end[:2],
              "SH": end[3:], "MAI": "10", "TUT": "1", "WEI": "100"}   # 枚数(CON)は 10×1 で決まる
    values.update(extra)
    return values


def rest(start: str, end: str, minutes: str = "60") -> dict:
    """休憩だけの行(停止の記号 0)。"""
    return {"KZ": start[:2], "KH": start[3:], "SZ": end[:2], "SH": end[3:],
            "S": "0", "TH": minutes}


def rows_of(*items: dict) -> dict:
    return {str(i): values for i, values in enumerate(items, start=1)}


def clean_shift_rows(shift: str, prefix: str) -> dict:
    """その直を**綺麗に**最後まで打った1ページ(休憩60分・定時まで)。"""
    start, end = {"1直": ("07:00", "15:00"), "2直": ("15:00", "22:50"),
                  "3直": ("22:50", "07:00"), "日勤": ("08:00", "17:00")}[shift]
    mid1, mid2 = {"1直": ("10:00", "11:00"), "2直": ("18:00", "19:00"),
                  "3直": ("02:00", "03:00"), "日勤": ("12:00", "13:00")}[shift]
    return rows_of(row(f"{prefix}01", start, mid1), rest(mid1, mid2),
                   row(f"{prefix}02", mid2, end))


def full_page_rows(prefix: str, start: str) -> dict:
    """12行ぜんぶ埋めた紙(次ページ発行ができる形)。1行10分、`start` から120分。"""
    items = []
    t = _real_datetime(2026, 1, 1, int(start[:2]), int(start[3:]))
    for n in range(1, 13):
        end = t + timedelta(minutes=10)
        items.append(row(f"{prefix}{n:02d}", t.strftime("%H:%M"), end.strftime("%H:%M")))
        t = end
    return rows_of(*items)


def lots_in(rows: dict) -> list[str]:
    return [values["LOT"] for _, values in sorted(rows.items(), key=lambda kv: int(kv[0]))
            if values.get("LOT")]


# ======================================================================
# 土台
# ======================================================================
@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class ScenarioCase(WebTestCase):
    """1件ごとに: 時計を止め、時間マスタ・共有・停止マスタを置く。"""

    def setUp(self) -> None:
        self._saved_env2 = {k: os.environ.get(k)
                            for k in ("NIPPOU_ACCESS_DIR", "NIPPOU_MONTHLY_DIR")}
        super().setUp()
        self.addCleanup(self._restore_env2)
        self.shared_dir = self.tmp / "shared"
        self.shared_dir.mkdir()
        os.environ["NIPPOU_ACCESS_DIR"] = str(self.shared_dir)
        os.environ["NIPPOU_MONTHLY_DIR"] = str(self.tmp / "monthly")
        from nippou import config, user_settings
        user_settings.save_many({config.KEY_REPORT_OUT_DIR: str(self.tmp / "出力")})
        # 停止の記号のマスタ(0=休憩食事, 2, イ, A)。無いと記号の確かめが黙る
        from tests.test_stop_reason_csv import make_transmission
        make_transmission(self.tmp / "ref" / "伝送用ファイル.sqlite3")
        self.repo().replace_shift_times(SHIFT_TIMES)
        _Now.value = _real_datetime(2026, 10, 5, 8, 0)
        install_clock(self)
        from nippou.services import shift_close
        shift_close.reset()
        self.addCleanup(shift_close.reset)
        self.opened: Optional[dict] = None

    def _restore_env2(self) -> None:
        for key, value in self._saved_env2.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # -- 時計 -----------------------------------------------------------
    def at(self, when: str) -> None:
        """'2026-10-05 14:55' の形で時計を合わせる。"""
        _Now.value = _real_datetime.strptime(when, "%Y-%m-%d %H:%M")

    # -- 画面でやること -------------------------------------------------
    @property
    def shared_path(self) -> Path:
        return self.shared_dir / "日報データ.sqlite3"

    def open_screen(self) -> dict:
        """日報入力を開く(描いた直がそのまま画面の直になる)。"""
        self.assertEqual(self.get("/").status_code, 200)
        body = self.post("/api/entry/state", {"rows": {}, "header": {}, "checks": {}}
                         ).get_json()
        self.remember(body)
        return body

    def remember(self, body: dict) -> None:
        if body and body.get("report_date") and body.get("shift"):
            self.opened = {"report_date": body["report_date"], "shift": body["shift"],
                           "page": str(body.get("page", 1)), "line": body.get("line", "")}

    def sheet(self, rows: dict, worker: str = "山田", **extra) -> dict:
        body = {"rows": rows, "header": {"worker": worker}, "checks": {}}
        if self.opened:
            body["opened"] = dict(self.opened)
        body.update(extra)
        return body

    def choose_worker(self, worker: str) -> dict:
        """「作業者を選ぶ」── ここで直が固定される。"""
        body = self.post("/api/entry/state", self.sheet({}, worker)).get_json()
        self.remember(body)
        return body

    def save(self, rows: dict, worker: str = "山田", **extra):
        """保存(確定)。応答をそのまま返す。"""
        res = self.post("/api/entry/save", self.sheet(rows, worker, **extra))
        if res.status_code == 200:
            self.remember(res.get_json())
        return res

    def autosave(self, rows: dict, worker: str = "山田"):
        return self.save(rows, worker, silent=True)

    def new_page(self, rows: dict, worker: str = "山田"):
        res = self.post("/api/entry/newpage", self.sheet(rows, worker, confirm=True))
        if res.status_code == 200:
            self.opened = dict(self.opened or {}, page=str(res.get_json()["page"]))
        return res

    def push(self, **body):
        """範囲を言わずに押す(まとめて送る ── 1つでも引っかかれば止める)。"""
        return self.post("/api/settings/push", body)

    def push_now(self, **body):
        """入力画面のふだんの「共有へ保存」── いまの直だけ。"""
        return self.post("/api/settings/push", dict(body, scope="current"))

    def push_past(self, **body):
        """赤い帯の「過去分を共有へ送る」── いまの直ではない未送信の直ぜんぶ。"""
        return self.post("/api/settings/push", dict(body, scope="previous"))

    def start_shift(self, when: str, worker: str) -> dict:
        """時計を合わせて、開いて、作業者を選ぶ(直の始まり)。"""
        self.at(when)
        self.open_screen()
        return self.choose_worker(worker)

    def restart_app(self) -> None:
        """窓を×で閉じて起動し直した(プロセスに1つのものが消える)。"""
        from nippou import work_context
        from nippou.services import shift_close
        work_context.reset()
        shift_close.reset()
        self.opened = None

    # -- 確かめるもの ---------------------------------------------------
    def shared_rows(self, table: str) -> list[dict]:
        if not self.shared_path.exists():
            return []
        conn = sqlite3.connect(self.shared_path)
        conn.row_factory = sqlite3.Row
        try:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                                  (table,)).fetchone()
            if not exists:
                return []
            return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        finally:
            conn.close()

    def shared_headers(self, line: str = "L-1") -> list[dict]:
        return self.shared_rows(f"T_日報ヘッダー_{line}")

    def shared_details(self, line: str = "L-1") -> list[dict]:
        return self.shared_rows(f"T_日報明細_{line}")

    def shared_keys(self, line: str = "L-1") -> set[tuple]:
        return {(h["報告日"], h["直"], int(h["ページ"])) for h in self.shared_headers(line)}

    def shared_lots(self, line: str = "L-1") -> dict[tuple, list[str]]:
        """(報告日, 直, ページ) → 打ってある LOT の並び(行番号順)。"""
        found: dict[tuple, list[tuple[int, str]]] = {}
        for d in self.shared_details(line):
            key = (d["報告日"], d["直"], int(d["ページ"]))
            found.setdefault(key, []).append((int(d["行番号"]), d["LOT"] or ""))
        return {k: [lot for _, lot in sorted(v) if lot] for k, v in found.items()}

    def local_lots(self, line: str = "L-1") -> dict[tuple, list[str]]:
        repo = self.repo()
        out = {}
        for day, ln, shift, page in repo.list_keys(line):
            _h, details = repo.load(day, ln, shift, page)
            out[(day, shift, page)] = [d.lot for d in sorted(details, key=lambda d: d.row_no)
                                       if d.lot]
        return out

    def dirty_keys(self) -> set[tuple]:
        return {(h.report_date, h.shift, h.page) for h in self.repo().pending_sync_headers()}

    def history(self) -> list[dict]:
        return self.repo().push_history()

    def shared_history(self) -> list[dict]:
        return self.shared_rows("T_共有保存履歴")

    # -- よく使う一続きの操作 ---------------------------------------------
    def close_tick(self, when: str) -> dict:
        """画面の1分タイマーが叩く「残り5分の自動確定」(と、終わった直の拾い)。"""
        self.at(when)
        res = self.post("/api/entry/close", {})
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def next_shift(self, when: str, worker: str) -> dict:
        """前の直の画面が見るだけになったあと、次の人が「次の直を始める」→作業者を選ぶ。"""
        self.at(when)
        self.assertEqual(self.post("/api/entry/start-next", {}).status_code, 200)
        self.opened = None
        self.open_screen()
        return self.choose_worker(worker)

    def take_over(self, worker: str) -> dict:
        res = self.post("/api/entry/handover", {"worker": worker})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def admin_on(self) -> None:
        res = self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())

    def recall(self, report_date: str, shift: str, page: int = 1, line: str = "L-1") -> dict:
        """「記録を見る」から呼び出す(画面はそのページを開き直す)。"""
        res = self.post("/api/settings/recall", {"report_date": report_date, "line": line,
                                                  "shift": shift, "page": page})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.opened = None
        return self.open_screen()

    def back(self) -> None:
        self.assertEqual(self.post("/api/settings/back", {}).status_code, 200)
        self.opened = None
        self.open_screen()

    def state(self, rows: Optional[dict] = None, worker: str = "") -> dict:
        body = self.sheet(rows or {}, worker)
        if not worker:
            body["header"] = {}
        return self.post("/api/entry/state", body).get_json()

    def shared_offline(self) -> None:
        """共有のフォルダに届かない(ネットワークが切れた・フォルダを動かした)。"""
        self.shared_dir.rename(self.tmp / "shared_off")

    def shared_online(self) -> None:
        (self.tmp / "shared_off").rename(self.shared_dir)

    def packing(self, report_date: str, shift: str, line: str = "L-1"):
        return self.repo().load_packing_report(report_date, line, shift)


# ======================================================================
# A. 直代わりのタイミング
# ======================================================================
class A1_CrossingTests(ScenarioCase):
    """直の終わり間際に打っていて、境をまたいでから保存する。

    作業者を選んだ時点で直が決まる(固定)。時計が15:00を過ぎても書き先は1直のまま。
    """

    def test_境の前の自動保存は1直へ入る(self) -> None:
        body = self.start_shift("2026-10-05 07:05", "山田")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月5日", "1直"))
        self.at("2026-10-05 14:58")
        res = self.autosave(clean_shift_rows("1直", "A"))
        self.assertTrue(res.get_json()["saved"])
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})

    def test_境を過ぎてからの保存確定と自動保存は断る_打ちかけは前の直へ置ける(self) -> None:
        """15:02 に「保存(確定)」を押しても断られ(見るだけ)、1直にも2直にも書かない。
        画面は見るだけへ塗り直す前に打ちかけを置く(draft)── それは**1直へ**入る。"""
        self.start_shift("2026-10-05 07:05", "山田")
        rows = clean_shift_rows("1直", "A")
        self.at("2026-10-05 15:02")
        for res in (self.save(rows), self.autosave(rows)):
            self.assertEqual(res.status_code, 403)
            self.assertIn("見るだけ", res.get_json()["message"])
        self.assertEqual(self.dirty_keys(), set(), "断ったのにどこかへ書いた")
        placed = self.save(rows, draft=True)
        self.assertEqual(placed.status_code, 200)
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        self.assertEqual(self.local_lots()[("2026年10月5日", "1直", 1)], ["A01", "A02"])

    def test_画面とサーバの直が食い違わない(self) -> None:
        """またいだあとも、画面(帯)・1分の見張り・書き先がそろって「1直」を指す。
        時計の直は帯の別の札(「いま 2直」)で知らせる。"""
        self.start_shift("2026-10-05 14:50", "山田")
        self.at("2026-10-05 15:01")
        body = self.state(clean_shift_rows("1直", "A"), "山田")
        watch = self.get("/api/shift/key?report_date=2026年10月5日&shift=1直&page=1").get_json()
        self.assertEqual(body["shift"], "1直")
        self.assertEqual(body["ribbon"]["shift"], "1直")
        self.assertEqual(watch["shift"], "1直")
        self.assertFalse(watch["shift_changed"]["crossed"])
        self.assertTrue(watch["anchor_standing"]["expired"])
        self.assertTrue(body["read_only"])
        self.assertEqual(body["read_only_reason"], "anchor")
        self.assertIn("いま 2直", [c["text"] for c in body["ribbon"]["chips"]])

    def test_直の始まる前に作業者を選ぶと前の直に固定される(self) -> None:
        """【要相談】06:55 に1直の人が作業者を選ぶと、**前日の3直**に固定される。
        打った行は前日3直へ入り、07:00 で見るだけになる(1直の紙ではない)。"""
        body = self.start_shift("2026-10-05 06:55", "早出")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月4日", "3直"))
        self.autosave(rows_of(row("E01", "07:00", "08:00")), "早出")
        self.assertEqual(self.dirty_keys(), {("2026年10月4日", "3直", 1)})
        self.at("2026-10-05 07:01")
        self.assertTrue(self.state(worker="早出")["read_only"])


class A2_ReminderTests(ScenarioCase):
    """残り15分の催促と、残り5分の自動確定。"""

    def test_15分前に催促が1度だけ出る(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.at("2026-10-05 14:44")
        self.assertIsNone(self.get("/api/sound/due").get_json()["play"])
        self.at("2026-10-05 14:46")
        play = self.get("/api/sound/due").get_json()["play"]
        self.assertEqual(play["key"], "print_reminder")
        self.assertIn("1直 終了14分前", play["message"])
        self.at("2026-10-05 14:47")
        self.assertIsNone(self.get("/api/sound/due").get_json()["play"], "1分ごとに鳴り続ける")

    def test_5分前に確定し_綺麗なら共有へも送る(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.assertFalse(self.close_tick("2026-10-05 14:54")["ran"])
        done = self.close_tick("2026-10-05 14:55")
        self.assertTrue(done["ran"])
        self.assertEqual(done["pushed"], 1)
        self.assertEqual(done["findings"], [])
        self.assertEqual(self.dirty_keys(), set())
        self.assertEqual(self.shared_lots()[("2026年10月5日", "1直", 1)], ["A01", "A02"])
        again = self.close_tick("2026-10-05 14:56")
        self.assertFalse(again["ran"])
        self.assertIn("確定済み", again["reason"])
        # 確定した直は催促も止まる
        self.at("2026-10-05 14:57")
        self.assertIsNone(self.get("/api/sound/due").get_json()["play"])

    def test_自動で送った直も共有保存の履歴に残る(self) -> None:
        """【v4.34.2 で直した】「送ったのだから履歴に載せるべきです」。
        押した人は「(自動確定)」、通し方は「残り5分の自動確定」。共有の表へも送った直後に写る。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.close_tick("2026-10-05 14:55")
        found = [(h["report_date"], h["shift"], h["result"], h["pressed_by"], h["via"],
                  h["pages"], h["worker"], h["pressed_key"]) for h in self.history()]
        self.assertEqual(found, [("2026年10月5日", "1直", "送れた", "(自動確定)",
                                  "残り5分の自動確定", 1, "山田", "2026年10月5日 1直")])
        self.assertTrue(self.history()[0]["pressed_at"].startswith("2026-10-05T14:55"))
        shared = [(r["直"], r["結果"], r["押した人"], r["通し方"]) for r in self.shared_history()]
        self.assertEqual(shared, [("1直", "送れた", "(自動確定)", "残り5分の自動確定")])

    def test_翌朝の拾いで送った直も履歴に残り_通し方で分かれる(self) -> None:
        """06:55 に画面が開いていなかった3直を、07:06 の1直の画面が拾って送る。"""
        self.start_shift("2026-10-05 22:55", "夜勤")
        self.at("2026-10-06 06:30")
        self.save(clean_shift_rows("3直", "C"), "夜勤")
        self.restart_app()
        self.at("2026-10-06 07:05")
        self.open_screen()
        self.close_tick("2026-10-06 07:06")
        found = [(h["report_date"], h["shift"], h["result"], h["pressed_by"], h["via"],
                  h["pressed_key"]) for h in self.history()]
        self.assertEqual(found, [("2026年10月5日", "3直", "送れた", "(自動確定)",
                                  "次の直の画面が拾って送った", "2026年10月6日 1直")])
        self.assertEqual([r["通し方"] for r in self.shared_history()],
                         ["次の直の画面が拾って送った"])

    def test_自動で送れなかったときも残り_届いてから写る(self) -> None:
        """共有に届かない日の自動確定は「送れなかった」で残り、共有の表へは届いてから写る。"""
        self.shared_offline()
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.close_tick("2026-10-05 14:55")
        self.assertEqual([(h["result"], h["pressed_by"], h["shared"]) for h in self.history()],
                         [("送れなかった", "(自動確定)", 0)])
        self.shared_online()
        self.assertEqual(self.push_now(worker="佐藤").status_code, 200)
        self.assertEqual([(r["結果"], r["押した人"]) for r in self.shared_history()],
                         [("送れなかった", "(自動確定)"), ("送れた", "佐藤")])

    def test_自動確定が関門で止めたときも手で押したときと同じに残す(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        rows = clean_shift_rows("1直", "A")
        rows["2"]["TH"] = "30"                         # 休憩30分
        self.at("2026-10-05 14:30")
        self.save(rows)
        self.close_tick("2026-10-05 14:55")
        self.assertEqual([(h["shift"], h["result"], h["pressed_by"], h["via"])
                          for h in self.history()],
                         [("1直", "関門で止めた", "(自動確定)", "残り5分の自動確定")])
        self.assertIn("休憩", self.history()[0]["detail"])

    def test_自動で送った直も標準作業時間へ入る(self) -> None:
        """「共有へ保存」で送った直は標準作業時間へ写る。自動確定で送った直も同じに写ること。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.close_tick("2026-10-05 14:55")
        from nippou.config import SETTINGS
        db = SETTINGS.standard_time_db_path
        self.assertTrue(db.exists(), "標準作業時間.sqlite3 へ1度も写していない")
        conn = sqlite3.connect(db)
        try:
            shifts = {tuple(r) for r in conn.execute('SELECT DISTINCT 報告日, 直 FROM "T_作業実績"')}
        finally:
            conn.close()
        self.assertIn(("2026年10月5日", "1直"), shifts)
        self.assertEqual(self.repo().standard_time_pending(), [])

    def test_直すところがあれば確定しても送らない(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        rows = clean_shift_rows("1直", "A")
        rows["2"]["TH"] = "30"                         # 休憩30分
        self.at("2026-10-05 14:30")
        self.save(rows)
        done = self.close_tick("2026-10-05 14:55")
        self.assertTrue(done["ran"])
        self.assertEqual(done["pushed"], 0)
        self.assertIn("休憩", done["findings"][0])
        self.assertIn("共有へは送っていません", done["message"])
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        self.assertEqual(self.shared_keys(), set())

    def test_確定のあとに打ち足すと未送信に戻る(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        rows = clean_shift_rows("1直", "A")
        self.save(rows)
        self.close_tick("2026-10-05 14:55")
        rows["1"]["MAI"] = "11"
        self.at("2026-10-05 14:58")
        self.assertTrue(self.save(rows).get_json()["saved"])
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        # 15:00 を過ぎると打てない。残った1直は次の直の始めに出る
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        self.assertTrue(body["handover"]["blocked"])
        self.assertTrue(body["handover"]["all_clean"])


class A3_RestartTests(ScenarioCase):
    """直の境の前後で窓を×で閉じて、起動し直す(固定はメモリーだけ)。"""

    def test_境の前に閉じた_打ちかけは1直に残り次の画面は2直と関門(self) -> None:
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:58")
        # ×で閉じる → 画面は打ちかけを置いてから閉じる(pagehide の draft)
        self.assertEqual(self.save(clean_shift_rows("1直", "A"), draft=True).status_code, 200)
        self.restart_app()
        self.at("2026-10-05 15:03")
        body = self.open_screen()
        self.assertEqual(body["shift"], "2直")
        self.assertTrue(body["needs_worker"])
        self.assertFalse(body["anchor"]["active"], "固定が残っている")
        self.assertTrue(body["handover"]["blocked"])
        self.assertIn("2026年10月5日 L-1 1直", body["handover"]["message"])
        self.assertTrue(body["handover"]["all_clean"])
        # 作業者を選ぶと2直に固定。保存(確定)は関門で止まる
        chosen = self.choose_worker("佐藤")
        self.assertEqual(chosen["anchor"]["shift"], "2直")
        res = self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "handover_required")
        # 赤い帯の「過去分を共有へ送る」1押しで通れる
        self.assertEqual(self.push_past(worker="佐藤").status_code, 200)
        self.assertEqual(self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤").status_code, 200)
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "1直", 1)})

    def test_境の後に閉じた_打ちかけは1直へ(self) -> None:
        """15:01 に閉じても、アプリが生きているうちの置き(draft)は固定した1直へ入る。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 15:01")
        self.assertEqual(self.save(clean_shift_rows("1直", "A"), draft=True).status_code, 200)
        self.restart_app()
        self.at("2026-10-05 15:10")
        body = self.open_screen()
        self.assertEqual(body["shift"], "2直")
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "2直"), [])

    def test_起動し直したあと_前の画面の送信は新しい直へ書かない(self) -> None:
        """起動し直す前に開いていた古いタブ(1直を名乗る)からの送信は、2直へ書かない。"""
        self.start_shift("2026-10-05 07:05", "山田")
        old = dict(self.opened)
        self.restart_app()
        self.start_shift("2026-10-05 15:05", "佐藤")
        self.opened = old
        res = self.save(rows_of(row("X01", "14:00", "14:30")), "山田", draft=True)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["saved"])
        self.assertTrue(res.get_json()["shift_changed"]["crossed"])
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "2直"), [])
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [])
        # 押した保存(確定)なら「どちらの直へ入れますか」を訊き返す(勝手に書かない)
        asked = self.save(rows_of(row("X01", "14:00", "14:30")), "山田")
        self.assertEqual(asked.status_code, 409)
        self.assertIn("どちらに入れるか", asked.get_json()["message"])


class A4_NightShiftTests(ScenarioCase):
    """3直(22:50〜07:00)── 報告日は始めた日。"""

    def test_0時をまたいだ保存も始めた日の3直(self) -> None:
        body = self.start_shift("2026-10-05 22:55", "夜勤")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月5日", "3直"))
        rows = clean_shift_rows("3直", "C")
        self.at("2026-10-05 23:50")
        self.autosave(rows, "夜勤")
        self.at("2026-10-06 00:10")
        res = self.save(rows, "夜勤")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.get_json()["report_date"], res.get_json()["shift"]),
                         ("2026年10月5日", "3直"))
        self.assertEqual(self.repo().list_keys("L-1"), [("2026年10月5日", "L-1", "3直", 1)])
        # 0時を過ぎてから開き直しても同じ直(固定が無くても時計で同じ答え)
        self.restart_app()
        self.at("2026-10-06 03:00")
        reopened = self.open_screen()
        self.assertEqual((reopened["report_date"], reopened["shift"]), ("2026年10月5日", "3直"))
        self.assertTrue(reopened["handover"]["any_left"] is False)

    def test_6時55分に確定して送り_7時で閉じる(self) -> None:
        self.start_shift("2026-10-05 22:55", "夜勤")
        self.at("2026-10-06 06:30")
        self.save(clean_shift_rows("3直", "C"), "夜勤")
        done = self.close_tick("2026-10-06 06:55")
        self.assertTrue(done["ran"])
        self.assertEqual(done["pushed"], 1)
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "3直", 1)})
        self.at("2026-10-06 06:59")
        self.assertFalse(self.state(worker="夜勤")["read_only"])
        self.at("2026-10-06 07:00")
        self.assertTrue(self.state(worker="夜勤")["read_only"])
        # 07:01 に次の人が始めると、その日の1直
        body = self.next_shift("2026-10-06 07:01", "山田")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月6日", "1直"))
        self.assertFalse(body["handover"]["any_left"])

    def test_確定し忘れた3直は翌朝の画面が拾う(self) -> None:
        """06:55 に画面が開いていなかった。07:05 に1直が開くと、終わった3直を確定して送る。"""
        self.start_shift("2026-10-05 22:55", "夜勤")
        self.at("2026-10-06 06:30")
        self.save(clean_shift_rows("3直", "C"), "夜勤")
        self.restart_app()
        self.at("2026-10-06 07:05")
        body = self.open_screen()
        self.assertTrue(body["handover"]["blocked"])
        tick = self.close_tick("2026-10-06 07:06")
        self.assertEqual([(s["report_date"], s["shift"]) for s in tick["swept"]],
                         [("2026年10月5日", "3直")])
        self.assertIn("共有へも送りました", tick["message"])
        self.assertFalse(self.open_screen()["handover"]["any_left"])
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "3直", 1)})


class A5_DayShiftTests(ScenarioCase):
    """日勤のライン(トット)。直は時刻にかかわらず「日勤」、報告日はその日。"""

    terminal_line = "トット"

    def day_rows(self, end: str = "17:00") -> dict:
        items = [row("D01", "08:00", "12:00"), rest("12:00", "13:00"),
                 row("D02", "13:00", "17:00")]
        if end != "17:00":
            items.append(row("D03", "17:00", end))
        return rows_of(*items)

    def test_日勤の流れ(self) -> None:
        body = self.start_shift("2026-10-05 08:05", "日勤者")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月5日", "日勤"))
        self.at("2026-10-05 16:30")
        self.assertEqual(self.save(self.day_rows()).status_code, 200)
        done = self.close_tick("2026-10-05 16:55")
        self.assertTrue(done["ran"])
        self.assertEqual(done["pushed"], 1)
        self.assertEqual(self.shared_keys("トット"), {("2026年10月5日", "日勤", 1)})
        self.assertEqual(self.shared_keys("L-1"), set())
        # 翌朝は翌日の日勤
        self.restart_app()
        body = self.start_shift("2026-10-06 07:30", "日勤者")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月6日", "日勤"))
        self.assertFalse(body["read_only"])

    def test_昼稼働は休憩を見ない(self) -> None:
        self.start_shift("2026-10-05 08:05", "日勤者")
        rows = rows_of(row("D01", "08:00", "17:00"))
        self.at("2026-10-05 16:30")
        self.assertEqual(self.save(rows, "日勤者", day_shift=True).status_code, 200)
        self.assertEqual(self.push_now().status_code, 200)
        self.assertEqual(self.shared_headers("トット")[0]["昼勤"], "有")
        # 昼稼働でなければ休憩60分が要る
        self.at("2026-10-05 16:40")
        self.save(rows, "日勤者")
        res = self.push_now()
        self.assertEqual(res.status_code, 422)
        self.assertIn("休憩", res.get_json()["error"]["message"])

    def test_日勤の残業は17時を過ぎても打てる(self) -> None:
        """【v4.34.2 で直した】確かめ(最終時間)は日勤の残業 18:00/19:00 を認めている。
        画面の固定も日勤だけは残業の終わり(19:00)まで開けておくので、残業の行を保存できる。
        「共有へ保存」も最後の行 19:00 で通る。"""
        self.start_shift("2026-10-05 08:05", "日勤者")
        self.at("2026-10-05 18:50")
        res = self.save(self.day_rows("19:00"))
        self.assertEqual(res.status_code, 200, res.get_json().get("message"))
        self.assertEqual((res.get_json()["report_date"], res.get_json()["shift"]),
                         ("2026年10月5日", "日勤"))
        self.assertEqual(self.push_now().status_code, 200)
        self.assertEqual(self.shared_lots("トット")[("2026年10月5日", "日勤", 1)],
                         ["D01", "D02", "D03"])

    def test_日勤の残業中は帯が残業の終わりまでを言い_画面とサーバがそろう(self) -> None:
        """17:00〜19:00 は開いたまま(見るだけにしない)。1分の見張りも同じ答えで、
        帯には「残業の行は19:00まで打てます」。19:00 ちょうどで見るだけ・保存も断る。"""
        self.start_shift("2026-10-05 08:05", "日勤者")
        self.at("2026-10-05 17:30")
        body = self.state(self.day_rows("18:00"), "日勤者")
        watch = self.get("/api/shift/key?report_date=2026年10月5日&shift=日勤&page=1"
                         ).get_json()
        self.assertFalse(body["read_only"])
        self.assertFalse(watch["anchor_standing"]["expired"])
        self.assertEqual(watch["anchor_standing"]["minutes_left"], 90)
        self.assertIn("残業の行は19:00まで打てます", watch["anchor_standing"]["message"])
        self.at("2026-10-05 19:00")
        closed = self.state(worker="日勤者")
        self.assertTrue(closed["read_only"])
        self.assertEqual(closed["read_only_reason"], "anchor")
        self.assertTrue(self.get("/api/shift/key").get_json()["anchor_standing"]["expired"])
        self.assertEqual(self.save(self.day_rows("19:00")).status_code, 403)

    def test_日勤の5分前の確定のあとに残業を打つと未送信に戻る(self) -> None:
        """5分前の自動確定は定時(16:55)で走って送る。そのあとの残業の行は未送信に戻り、
        翌朝の引き継ぎに「共有へ保存」1押しで出る(1直で打ち足したときと同じ)。"""
        self.start_shift("2026-10-05 08:05", "日勤者")
        self.at("2026-10-05 16:30")
        self.save(self.day_rows())
        self.assertEqual(self.close_tick("2026-10-05 16:55")["pushed"], 1)
        self.at("2026-10-05 18:00")
        self.assertEqual(self.save(self.day_rows("18:00")).status_code, 200)
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "日勤", 1)})
        self.assertFalse(self.close_tick("2026-10-05 18:01")["ran"])
        # 翌朝: 前の日の日勤はとっくに閉じている。次の直を始めると、その日の日勤
        body = self.next_shift("2026-10-06 07:30", "日勤者")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月6日", "日勤"))
        self.assertTrue(body["handover"]["blocked"])
        self.assertTrue(body["handover"]["all_clean"])

    def test_日勤ラインで17時過ぎに開き直してもその日の日勤で19時まで(self) -> None:
        """起動し直して 17:30 に作業者を選ぶと、その日の日勤に固定され、残業の終わりまで打てる。
        19:00 を過ぎてから選ぶと、その日の日勤に固定されてすぐ見るだけ(翌日の紙にはしない)。"""
        self.restart_app()
        body = self.start_shift("2026-10-05 17:30", "日勤者")
        self.assertEqual((body["report_date"], body["shift"]), ("2026年10月5日", "日勤"))
        self.assertFalse(body["read_only"])
        self.restart_app()
        late = self.start_shift("2026-10-05 19:10", "日勤者")
        self.assertEqual((late["report_date"], late["shift"]), ("2026年10月5日", "日勤"))
        self.assertTrue(late["read_only"])


# ======================================================================
# B. 保存の有無
# ======================================================================
class B_SaveOrNotTests(ScenarioCase):

    def test_B1_打ちかけ自動保存だけのまま次の直へ(self) -> None:
        """1直は12時までしか打っておらず(休憩も無い)、自動保存だけで帰った。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 11:50")
        self.autosave(rows_of(row("A01", "07:00", "12:00")))
        closed = self.close_tick("2026-10-05 14:55")
        self.assertTrue(closed["ran"])
        self.assertEqual(closed["pushed"], 0)
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        gate = body["handover"]
        self.assertTrue(gate["blocked"])
        self.assertFalse(gate["all_clean"])
        self.assertEqual(gate["actions"], ["push", "take"])
        joined = "\n".join(gate["problems"])
        self.assertIn("最終時間まで入力がない", joined)
        self.assertIn("休憩", joined)
        # 押しても通らない(前の直のぶんは「設備移動のため保存」でも通せない)
        self.assertEqual(self.push_past(worker="佐藤").status_code, 422)
        self.assertEqual(self.push_past(worker="佐藤", skip=SKIP).status_code, 422)
        # 引き継げば始められる。1直は手元に未送信のまま残る
        self.take_over("佐藤")
        self.assertEqual(self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤").status_code, 200)
        self.assertIn(("2026年10月5日", "1直", 1), self.dirty_keys())
        self.assertEqual(self.shared_keys(), set())
        band = self.state(worker="佐藤")["handover"]
        self.assertTrue(band["any_left"])
        self.assertFalse(band["blocked"])
        self.assertIn("佐藤 が引き継ぎました", band["notes"][0])
        # 14:55 の自動確定が関門で止めたぶんも残る(押した人「(自動確定)」。v4.34.2)
        results = [(h["shift"], h["result"], h["pressed_by"]) for h in self.history()]
        self.assertEqual(results, [("1直", "関門で止めた", "(自動確定)"),
                                   ("1直", "関門で止めた", "佐藤"), ("1直", "関門で止めた", "佐藤"),
                                   ("1直", "直せないまま引き継いだ", "佐藤")])

    def test_B2_何も打たずに直が終わった(self) -> None:
        """空の直は、紙も未送信も作らない。次の直に「前の直が空」を一度だけ知らせる。"""
        # 前の日は1直を回している(回していない直の「空」は知らせない)
        self.start_shift("2026-10-04 07:05", "山田")
        self.at("2026-10-04 14:30")
        self.save(clean_shift_rows("1直", "Z"))
        self.close_tick("2026-10-04 14:55")
        # 10/5 の1直: 作業者だけ選んで何も打たない
        self.restart_app()
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 07:10")
        skipped = self.autosave({})
        self.assertFalse(skipped.get_json()["saved"])
        self.assertIn("まだ1行も打っていない", skipped.get_json()["skipped"])
        refused = self.save({})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("まだ1行も打っていません", refused.get_json()["message"])
        closed = self.close_tick("2026-10-05 14:55")
        self.assertFalse(closed["ran"])
        self.assertIn("打ってあるものがありません", closed["reason"])
        self.assertEqual(self.repo().saved_pages("2026年10月5日", "L-1", "1直"), [])
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        self.assertFalse(body["handover"]["any_left"], "空の直で関門が立った")
        self.assertTrue(body["prev_shift"]["warn"])
        self.assertEqual(self.dirty_keys(), set())

    def test_B3_保存確定はしたが共有へ保存しなかった(self) -> None:
        """14:50 に画面を閉じて帰った(自動確定の5分前に画面が無い)。
        次の直の画面が開くと関門が立つが、1分以内の見張りが拾って共有へ送る。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.assertEqual(self.save(clean_shift_rows("1直", "A")).status_code, 200)
        self.restart_app()
        self.at("2026-10-05 15:05")
        body = self.open_screen()
        self.assertTrue(body["handover"]["blocked"])
        self.assertTrue(body["handover"]["all_clean"])
        tick = self.close_tick("2026-10-05 15:06")
        self.assertEqual([s["shift"] for s in tick["swept"]], ["1直"])
        self.assertEqual(tick["swept"][0]["pushed"], 1)
        self.assertFalse(self.open_screen()["handover"]["any_left"])
        self.assertEqual(self.shared_lots(), {("2026年10月5日", "1直", 1): ["A01", "A02"]})
        self.assertFalse(self.packing("2026年10月5日", "1直").dirty)
        self.assertEqual(len(self.shared_rows("T_日報集計_L-1")), 1)

    def test_B4_前の直を呼び出して直し保存し直した(self) -> None:
        """送ったあとで管理者が直すと、未送信に戻る。次の「共有へ保存」で共有が置き換わる。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        rows = clean_shift_rows("1直", "A")
        self.save(rows)
        self.close_tick("2026-10-05 14:55")
        self.assertEqual(self.dirty_keys(), set())
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤")
        # 作業者は前の直を開けない
        self.at("2026-10-05 16:00")
        res = self.post("/api/settings/recall", {"report_date": "2026年10月5日",
                                                  "line": "L-1", "shift": "1直", "page": 1})
        self.assertEqual(res.status_code, 403)
        # 管理者が直す
        self.admin_on()
        opened = self.recall("2026年10月5日", "1直")
        self.assertFalse(opened["read_only"])
        fixed = clean_shift_rows("1直", "A")
        fixed["1"]["MAI"] = "20"                     # 枚数 20
        fixed["3"]["LOT"] = "A09"
        self.assertEqual(self.save(fixed, "山田").status_code, 200)
        self.assertIn(("2026年10月5日", "1直", 1), self.dirty_keys())
        self.assertEqual(self.push_past().status_code, 422, "呼出中は共有へ保存を断る")
        self.back()
        band = self.state(worker="佐藤")["handover"]
        self.assertTrue(band["any_left"])
        # 「過去分を共有へ送る」は いまの2直(途中)に引っかからない ── 一文は要らない(v4.35.0)
        res = self.push_past(worker="佐藤")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.shared_lots()[("2026年10月5日", "1直", 1)], ["A01", "A09"])
        self.assertNotIn(("2026年10月5日", "2直", 1), self.shared_keys(), "途中の2直まで出た")
        self.assertIn(("2026年10月5日", "2直", 1), self.dirty_keys())
        shared = {d["行番号"]: d for d in self.shared_details()
                  if (d["報告日"], d["直"]) == ("2026年10月5日", "1直")}
        self.assertEqual(len(shared), 12, "行が増えた・減った")
        self.assertEqual(shared[1]["CON"], "20")
        summary = [r for r in self.shared_rows("T_日報集計_L-1") if r["直"] == "1直"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(float(summary[0]["枚数"]), 30.0)


# ======================================================================
# C. エラーがあるとき
# ======================================================================
def all_errors_rows() -> dict:
    """前の直に残す4種類: 梱包数>検入枚数・停止の記号がマスタに無い・休憩不足・最終時間まで無い。"""
    return rows_of(row("A01", "07:00", "10:00", KEN="5", MAI="10", TUT="1"),
                   rest("10:00", "10:30", "30"),
                   row("A02", "10:30", "13:00", S="Z", TH="10"))


class C_ErrorTests(ScenarioCase):

    def leave_errors(self) -> None:
        """1直が4種類の間違いを残して帰る(保存(確定)は断られるので自動保存だけ残る)。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 13:00")
        refused = self.save(all_errors_rows())
        self.assertEqual(refused.status_code, 422)
        codes = {f["code"] for f in refused.get_json()["check_findings"]}
        self.assertEqual(codes, {"pack_over", "unknown_stop"}, "保存(確定)は打っている途中の間違いだけ止める")
        self.assertTrue(self.autosave(all_errors_rows()).get_json()["saved"])

    def test_C1_直すところを残したまま次の直へ(self) -> None:
        self.leave_errors()
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        gate = body["handover"]
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["actions"], ["push", "take"])
        joined = "\n".join(gate["problems"])
        for word in ("梱包数 10 が検入枚数 5", "停止内訳にない記号です", "休憩", "最終時間まで入力がない"):
            self.assertIn(word, joined)
        self.assertTrue(all(p.startswith("2026年10月5日 L-1 1直") for p in gate["problems"]))
        # 保存(確定)・次ページ発行は止まる
        self.assertEqual(self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤").status_code, 422)
        # 「過去分を共有へ送る」は断る。断りには直す場所(どの直の何ページ)と開く道が付く
        res = self.push_past(worker="佐藤")
        self.assertEqual(res.status_code, 422)
        blockers = res.get_json()["skip_blockers"]
        self.assertEqual({b["code"] for b in blockers},
                         {"pack_over", "unknown_stop", "short_break", "shift_end"})
        self.assertTrue(all(b["at"]["shift"] == "1直" for b in blockers))
        self.assertFalse(res.get_json()["skippable_only"])
        # 引き継げば始められる。帯は残る
        taken = self.take_over("佐藤")
        self.assertIn("4件", taken["message"])
        self.assertEqual(self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤").status_code, 200)
        found = self.repo().handover_of("2026年10月5日", "L-1", "1直")
        self.assertEqual(found["taken_by"], "佐藤")
        self.assertIn("梱包数", found["findings"])
        band = self.state(worker="佐藤")["handover"]
        self.assertTrue(band["any_left"])
        self.assertFalse(band["blocked"])

    def test_C2_設備移動のため保存はいまの直のボタンだけ_過去分は管理者(self) -> None:
        """【v4.35.0】ふだんの「共有へ保存」は いまの直だけを確かめるので、一文で通すのも
        いまの直だけ。過去分(1直)の休憩・最終時間は、その直の人か管理者でないと通せない。"""
        # 1直: 休憩と最終時間だけが足りない(通せる種類だけ)
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 12:00")
        self.save(rows_of(row("A01", "07:00", "12:00")))
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        self.assertEqual(body["handover"]["actions"], ["push", "take"])
        self.take_over("佐藤")
        # 2直: 途中まで(18:00まで)保存
        self.at("2026-10-05 18:00")
        self.save(rows_of(row("B01", "15:00", "18:00")), "佐藤")
        # ふだんの「共有へ保存」: 確かめるのは いまの2直だけ(1直は見ない)。途中なので断る
        res = self.push_now(worker="佐藤")
        self.assertEqual(res.status_code, 422)
        self.assertEqual([r["shift"] for r in res.get_json()["reports"]], ["2直"])
        self.assertIn("赤い帯の「過去分を共有へ送る」から送れます", res.get_json()["error"]["message"])
        # 一文を添えれば いまの2直だけ通る。1直は送らない
        res = self.push_now(worker="佐藤", skip=SKIP)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["skipped"], 2)
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "2直", 1)})
        # 「過去分を共有へ送る」: 1直のぶんは一文でも通せない(その直の人か管理者だけ)
        for extra in ({}, {"skip": SKIP}):
            res = self.push_past(worker="佐藤", **extra)
            self.assertEqual(res.status_code, 422)
            self.assertEqual([r["shift"] for r in res.get_json()["reports"] if not r["ok"]], ["1直"])
            self.assertTrue(any("前の直のぶんです" in b["message"]
                                for b in res.get_json()["skip_blockers"]))
            self.assertIn("過去分: 送った直 0件 / エラーで残した直 1件",
                          res.get_json()["error"]["message"])
        # 管理者なら過去分のぶんも一文で通せる
        self.admin_on()
        res = self.push_past(worker="佐藤", skip=SKIP)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["skipped"], 2)
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "1直", 1), ("2026年10月5日", "2直", 1)})
        # 共有保存の履歴の「通し方」で、どちらのボタンで押したか分かる
        self.assertEqual([(h["shift"], h["via"]) for h in self.history() if h["result"] == "送れた"],
                         [("2直", "いまの直・設備移動のため保存"), ("1直", "過去分・設備移動のため保存")])
        self.assertEqual([r["通し方"] for r in self.shared_history() if r["結果"] == "送れた"],
                         ["いまの直・設備移動のため保存", "過去分・設備移動のため保存"])

    def test_C2_共有に届かず引き継いだあと_1直は過去分のボタン_2直はふだんのボタンで送れる(self) -> None:
        """【v4.35.0 で直した】共有に届かない日に1直(綺麗)を引き継いで2直を始め、18:00 に
        届くようになった。前は帯の「共有へ保存する」を押すと**いまの2直が途中**(最終時間・
        休憩がまだ)なので断られ、1直も送れなかった。いまは赤い帯の「過去分を共有へ送る」で
        1直だけが送れ、2直は直の終わりにふだんの「共有へ保存」で送れる。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.shared_offline()
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.assertEqual(self.push_past(worker="佐藤").get_json()["failed"], 1)
        self.take_over("佐藤")
        self.at("2026-10-05 16:00")
        self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤")
        self.at("2026-10-05 18:00")
        self.shared_online()
        band = self.state(worker="佐藤")["handover"]
        self.assertFalse(band["blocked"])
        self.assertIn("「過去分を共有へ送る」を押せば済みます", band["message"])
        # 赤い帯のボタン: 1直だけ送る。途中の2直には引っかからず、2直は送らない
        res = self.push_past(worker="佐藤")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("過去分: 送った直 1件 / エラーで残した直 0件", res.get_json()["message"])
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "1直", 1)})
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "2直", 1)})
        self.assertFalse(self.state(worker="佐藤")["handover"]["any_left"], "1直が帯に残っている")
        # 集計も1直のぶんだけ(途中の2直の数を共有へ出さない)
        self.assertEqual([r["直"] for r in self.shared_rows("T_日報集計_L-1")], ["1直"])
        # ふだんのボタンを直の途中で押すと、いまの2直の最終時間・休憩で今までどおり断る
        res = self.push_now(worker="佐藤")
        self.assertEqual(res.status_code, 422)
        codes = {f["code"] for r in res.get_json()["reports"] for f in r["findings"]}
        self.assertEqual(codes, {"shift_end", "short_break"})
        self.assertNotIn("過去分を共有へ送る", res.get_json()["error"]["message"],
                         "前の直はもう無いのに案内している")
        # 2直の終わりに打ち終えて押せば送れる
        self.at("2026-10-05 22:30")
        self.save(clean_shift_rows("2直", "B"), "佐藤")
        res = self.push_now(worker="佐藤")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "1直", 1), ("2026年10月5日", "2直", 1)})
        self.assertEqual([(h["shift"], h["via"]) for h in self.history() if h["result"] == "送れた"],
                         [("1直", "過去分"), ("2直", "いまの直")])

    def test_C2_いまの直のボタンは過去分のエラーに止められない(self) -> None:
        """【v4.35.0】前の直が未送信・エラーのままでも、いまの直は送れる(前の直は帯に残る)。"""
        self.leave_errors()
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.take_over("佐藤")
        self.at("2026-10-05 22:30")
        self.assertEqual(self.save(clean_shift_rows("2直", "B"), "佐藤").status_code, 200)
        res = self.push_now(worker="佐藤")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("共有へ保存(いまの直): 1件", res.get_json()["message"])
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "2直", 1)})
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        band = self.state(worker="佐藤")["handover"]
        self.assertEqual([x["shift"] for x in band["left"]], ["1直"])

    def test_C2_通せない間違いは管理者の一文でも通らない(self) -> None:
        self.leave_errors()
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.admin_on()
        res = self.push_past(skip=SKIP)
        self.assertEqual(res.status_code, 422)
        codes = {b["code"] for b in res.get_json()["skip_blockers"]}
        self.assertEqual(codes, {"pack_over", "unknown_stop"})

    def test_C3_エラーのある直が積み重なっても_綺麗な直は出る(self) -> None:
        """3直(10/4)が梱包数と休憩を残し、1直も最終時間を残し、2直が来た。
        【v4.35.0 で直した】前は綺麗に打った2直も古い直に止められて共有へ出なかった。
        いまは残り5分の自動確定が終わる直(いまの2直)だけを確かめて送る。"""
        self.start_shift("2026-10-04 22:55", "夜勤")
        bad3 = clean_shift_rows("3直", "C")
        bad3["1"].update(KEN="5", MAI="10", TUT="1")
        bad3["2"]["TH"] = "30"
        self.at("2026-10-05 06:50")
        self.autosave(bad3, "夜勤")
        self.assertEqual(self.close_tick("2026-10-05 06:55")["pushed"], 0)
        # 1直: 3直を引き継いで始める
        body = self.next_shift("2026-10-05 07:05", "山田")
        self.assertTrue(body["handover"]["blocked"])
        self.assertEqual(self.push_past(worker="山田").status_code, 422)
        self.take_over("山田")
        bad1 = clean_shift_rows("1直", "A")
        bad1["3"].update(SZ="14", SH="00")                    # 14:00 で終わっている
        self.at("2026-10-05 14:00")
        self.assertEqual(self.save(bad1).status_code, 200)
        closed = self.close_tick("2026-10-05 14:55")
        self.assertEqual(closed["pushed"], 0)
        self.assertIn("(2026年10月5日 L-1 1直)", closed["push_note"])
        self.assertNotIn("3直", closed["push_note"], "終わる直の自動確定が前の直まで見ている")
        # 2直: 2直ぶんが並んで出る
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        gate = body["handover"]
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["about"], "10月4日 3直 ほか1直")
        self.assertIn("(2直ぶん)", gate["message"])
        self.assertIn("直すところが 3件", gate["message"])
        self.assertEqual(len(gate["notes"]), 1, "引き継ぎ済みの3直の一行")
        res = self.push_past(worker="佐藤")
        self.assertEqual(res.status_code, 422)
        message = res.get_json()["error"]["message"]
        self.assertIn("過去分: 送った直 0件 / エラーで残した直 2件", message)
        self.assertIn("■ 2026年10月4日 L-1 3直", message)
        self.assertIn("■ 2026年10月5日 L-1 1直", message)
        self.take_over("佐藤")
        # 2直は綺麗に打てば、古い直に止められずに残り5分の自動確定で共有へ出る
        self.at("2026-10-05 22:30")
        self.assertEqual(self.save(clean_shift_rows("2直", "B"), "佐藤").status_code, 200)
        closed = self.close_tick("2026-10-05 22:45")
        self.assertEqual(closed["findings"], [])
        self.assertEqual(closed["pushed"], 1)
        self.assertEqual(self.dirty_keys(), {("2026年10月4日", "3直", 1), ("2026年10月5日", "1直", 1)})
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "2直", 1)})
        # 引き継ぎは**最後に引き継いだ人**だけが残る(3直は 山田 → 佐藤 に替わる)
        self.assertEqual(self.repo().handover_of("2026年10月4日", "L-1", "3直")["taken_by"], "佐藤")
        self.assertEqual(self.repo().handover_of("2026年10月5日", "L-1", "1直")["taken_by"], "佐藤")
        # 履歴には押した回数ぶん・引き継いだ回数ぶん残る(誰が引き継いだかの順も)
        handed = [(h["report_date"], h["shift"], h["pressed_by"]) for h in self.history()
                  if h["result"] == "直せないまま引き継いだ"]
        self.assertEqual(handed, [("2026年10月4日", "3直", "山田"), ("2026年10月4日", "3直", "佐藤"),
                                  ("2026年10月5日", "1直", "佐藤")])
        blocked = [h for h in self.history() if h["result"] == "関門で止めた"]
        self.assertEqual({h["shift"] for h in blocked}, {"3直", "1直"})
        self.assertEqual([(h["shift"], h["pressed_by"], h["via"]) for h in self.history()
                          if h["result"] == "送れた"],
                         [("2直", "(自動確定)", "残り5分の自動確定")])
        # 共有に届いたので、溜まっていた履歴の行もまとめて共有の表へ写る
        self.assertTrue(all(h["shared"] for h in self.history()))
        self.assertEqual(len(self.shared_history()), len(self.history()))

    def test_C4_引き継いだあとに直して共有へ保存(self) -> None:
        self.start_shift("2026-10-04 22:55", "夜勤")
        bad3 = clean_shift_rows("3直", "C")
        bad3["1"].update(KEN="5", MAI="10", TUT="1")
        self.at("2026-10-05 06:50")
        self.autosave(bad3, "夜勤")
        self.next_shift("2026-10-05 07:05", "山田")
        self.take_over("山田")
        self.at("2026-10-05 08:00")
        self.save(clean_shift_rows("1直", "A"))
        # 管理者が呼び出して直す
        self.at("2026-10-05 10:00")
        self.admin_on()
        opened = self.recall("2026年10月4日", "3直")
        self.assertFalse(opened["handover"]["blocked"])
        fixed = clean_shift_rows("3直", "C")
        fixed["1"].update(KEN="50", MAI="10", TUT="1")
        self.assertEqual(self.save(fixed, "夜勤").status_code, 200)
        self.back()
        band = self.state(worker="山田")["handover"]
        self.assertTrue(band["all_clean"], "直したのに断りが残っている")
        # 「過去分を共有へ送る」で送るのは3直だけ。いまの1直は直の終わりに
        res = self.push_past(worker="山田")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        self.assertFalse(self.state(worker="山田")["handover"]["any_left"], "帯が消えない")
        self.assertEqual(self.shared_keys(), {("2026年10月4日", "3直", 1)})
        # 引き継ぎの記録は消さない(誰が何を残して引き継いだか)
        self.assertEqual(self.repo().handover_of("2026年10月4日", "L-1", "3直")["taken_by"], "山田")
        self.at("2026-10-05 14:40")
        self.assertEqual(self.push_now(worker="山田").status_code, 200)
        self.assertEqual(self.dirty_keys(), set())
        shared = [(r["直"], r["結果"], r["通し方"]) for r in self.shared_history()]
        self.assertEqual(shared, [("3直", "直せないまま引き継いだ", ""), ("3直", "送れた", "過去分"),
                                  ("1直", "送れた", "いまの直")])


# ======================================================================
# D. 共有保存なしで進める・まとめて送る
# ======================================================================
def night_pages(prefix: str, count: int) -> list[dict]:
    """3直を `count` ページで(最後のページに休憩と定時まで)。"""
    pages, start = [], "22:50"
    for n in range(1, count):
        pages.append(full_page_rows(f"{prefix}P{n}", start))
        hour = (int(start[:2]) + 2) % 24
        start = f"{hour:02d}:{start[3:]}"
    rest_end = f"{(int(start[:2]) + 1) % 24:02d}:{start[3:]}"
    pages.append(rows_of(rest(start, rest_end), row(f"{prefix}P{count}01", rest_end, "07:00")))
    return pages


def second_pages(prefix: str) -> list[dict]:
    """2直を2ページで。"""
    return [full_page_rows(f"{prefix}P1", "15:00"),
            rows_of(row(f"{prefix}P201", "17:00", "19:00"), rest("19:00", "20:00"),
                    row(f"{prefix}P202", "20:00", "22:50"))]


class D_PushTogetherTests(ScenarioCase):
    """共有へ届かないまま何直も進み、あとで1回の「共有へ保存」で送る。"""

    def work(self, day: str, shift: str, worker: str, pages: list[dict], *,
             first: bool = False, take: bool = True) -> None:
        """1直ぶんを打つ(ページの数だけ次ページ発行 → 最後は保存(確定))と、5分前の自動確定。"""
        start = {"1直": "07:05", "2直": "15:05", "3直": "22:55"}[shift]
        if first:
            self.start_shift(f"{day} {start}", worker)
        else:
            body = self.next_shift(f"{day} {start}", worker)
            self.assertTrue(body["handover"]["blocked"])
            if take:
                self.take_over(worker)
        base = _real_datetime.strptime(f"{day} {start}", "%Y-%m-%d %H:%M")
        for n, rows in enumerate(pages, start=1):
            self.at((base + timedelta(minutes=110 * n)).strftime("%Y-%m-%d %H:%M"))
            if n < len(pages):
                res = self.new_page(rows, worker)
            else:
                res = self.save(rows, worker)
            self.assertEqual(res.status_code, 200, (shift, n, res.get_json()))
        end = base + timedelta(minutes={"1直": 470, "2直": 460, "3直": 480}[shift])
        self.close_tick(end.strftime("%Y-%m-%d %H:%M"))

    # 6直・2日ぶん。ページ数は 1,2,3,1,1,2
    PLAN = [("2026-10-05", "1直", "山田", [clean_shift_rows("1直", "DA")]),
            ("2026-10-05", "2直", "佐藤", second_pages("DB")),
            ("2026-10-05", "3直", "夜勤", night_pages("DC", 3)),
            ("2026-10-06", "1直", "鈴木", [clean_shift_rows("1直", "EA")]),
            ("2026-10-06", "2直", "高橋", [clean_shift_rows("2直", "EB")]),
            ("2026-10-06", "3直", "田中", night_pages("EC", 2))]

    def expected(self) -> dict[tuple, list[str]]:
        found = {}
        for day, shift, _worker, pages in self.PLAN:
            report = f"2026年10月{int(day[-2:])}日"
            for n, rows in enumerate(pages, start=1):
                found[(report, shift, n)] = lots_in(rows)
        return found

    def accumulate(self) -> None:
        self.shared_offline()
        for i, (day, shift, worker, pages) in enumerate(self.PLAN):
            self.work(day, shift, worker, pages, first=(i == 0))

    def test_D1_6直2日ぶんを1回で送る(self) -> None:
        self.accumulate()
        expected = self.expected()
        self.assertEqual(len(expected), 10)
        self.assertEqual(self.dirty_keys(), set(expected))
        self.assertEqual(self.local_lots(), expected)
        # 3日目の朝、共有に届くようになった。1直が開いて「共有へ保存」を1回
        self.shared_online()
        body = self.next_shift("2026-10-07 07:05", "山田")
        gate = body["handover"]
        self.assertEqual(len(gate["left"]), 6)
        self.assertTrue(gate["blocked"], "直前の3直はまだ誰も引き継いでいない")
        self.assertTrue(gate["all_clean"])
        self.assertEqual(len(gate["notes"]), 5, "引き継ぎ済みの5直ぶんの一行")
        res = self.push_past(worker="山田")               # 赤い帯の「過去分を共有へ送る」1押し
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["succeeded"], 10)
        self.assertIn("過去分: 送った直 6件 / エラーで残した直 0件", res.get_json()["message"])
        self.assertEqual(res.get_json()["failed"], 0)
        # 全部の直・全ページ・全行が、正しい鍵で、重ならず欠けずに入る
        self.assertEqual(self.shared_lots(), expected)
        headers = self.shared_headers()
        details = self.shared_details()
        self.assertEqual(len(headers), 10)
        self.assertEqual(len(details), 10 * 12)
        detail_keys = {(d["報告日"], d["直"], int(d["ページ"]), int(d["行番号"])) for d in details}
        self.assertEqual(len(detail_keys), 120, "同じ行が2つある")
        self.assertEqual({k[:3] for k in detail_keys}, self.shared_keys(), "ヘッダーと明細が対応しない")
        self.assertTrue(all(d["ライン"] == "L-1" for d in details))
        self.assertEqual(self.shared_rows("T_日報ヘッダー_LVC"), [])
        # 作業者名はその直の人
        workers = {(h["報告日"], h["直"]): h["担当者"] for h in headers}
        for day, shift, worker, _pages in self.PLAN:
            self.assertEqual(workers[(f"2026年10月{int(day[-2:])}日", shift)], worker)
        # 停止の欄(休憩 0 / 60分)が直ごとに1つずつ
        rests = sorted((d["報告日"], d["直"]) for d in details if d["S"] == "0" and d["TH"] == "60")
        self.assertEqual(len(rests), 6)
        self.assertEqual(len(set(rests)), 6)
        # 手元は全部送り済み、引き継ぎの帯も消える
        self.assertEqual(self.dirty_keys(), set())
        self.assertFalse(self.state(worker="山田")["handover"]["any_left"])
        # 履歴: 送れたが直ごとに1行(ページ数・担当者つき)
        sent = {(h["report_date"], h["shift"]): h for h in self.history() if h["result"] == "送れた"}
        self.assertEqual(len(sent), 6)
        self.assertEqual(sent[("2026年10月5日", "3直")]["pages"], 3)
        self.assertEqual(sent[("2026年10月5日", "2直")]["worker"], "佐藤")
        self.assertEqual({h["pressed_by"] for h in sent.values()}, {"山田"})
        self.assertEqual(len([r for r in self.shared_history() if r["結果"] == "送れた"]), 6)
        # 集計(D7): 手元の集計 = 共有の集計 = 共有の明細の枚数の合計
        summaries = {(r["報告日"], r["直"]): r for r in self.shared_rows("T_日報集計_L-1")}
        self.assertEqual(len(summaries), 6)
        for day, shift, _worker, pages in self.PLAN:
            report = f"2026年10月{int(day[-2:])}日"
            local = self.packing(report, shift)
            counted = sum(float(d["CON"] or 0) for d in details
                          if (d["報告日"], d["直"]) == (report, shift))
            self.assertEqual(local.pages, len(pages))
            self.assertEqual(local.total_quantity, counted, (report, shift))
            self.assertEqual(float(summaries[(report, shift)]["枚数"]), counted)
            self.assertEqual(int(summaries[(report, shift)]["ページ数"]), len(pages))
            self.assertFalse(local.dirty)
        # もう1回押しても増えない
        again = self.push_past(worker="山田")
        self.assertEqual(again.get_json()["succeeded"], 0)
        self.assertIn("送るものはありませんでした", again.get_json()["message"])
        self.assertEqual(len(self.shared_details()), 120)

    def test_D1_共有に届かない間も各直の自動確定は試して失敗を言う(self) -> None:
        self.shared_offline()
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        done = self.close_tick("2026-10-05 14:55")
        self.assertTrue(done["ran"])
        self.assertIn("送れなかった", done["push_note"])
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "1直", 1)})
        self.assertFalse((self.tmp / "shared_off" / "日報データ.sqlite3").exists())

    def test_共有に届かないとき_綺麗な前の直だけでも次の直を始められる(self) -> None:
        """前の直は綺麗(押し忘れだけ)だが、共有のフォルダに届かない。
        出口が「共有へ保存」だけだと、押しても失敗し、次の直が始められない。"""
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.shared_offline()
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        self.assertTrue(body["handover"]["blocked"])
        res = self.push_past(worker="佐藤")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["failed"], 1)
        gate = self.open_screen()["handover"]
        self.assertTrue(gate["blocked"])
        self.assertIn("take", gate["actions"], "押しても届かないのに、引き継ぐ道が出ていない")
        self.assertIn("共有へ届きませんでした", gate["message"])
        self.take_over("佐藤")
        self.assertEqual(self.save(rows_of(row("B01", "15:00", "16:00")), "佐藤").status_code, 200)

    def test_D2_エラーの直が混ざっても_送れる直は送り_エラーの直だけ残す(self) -> None:
        """【v4.35.0 で直した】前は2直のエラー1つで、1直・3直まで共有へ出なかった。
        「過去分を共有へ送る」は、エラーのある2直だけ帯に残して、ほかの直を送る。"""
        self.shared_offline()
        self.work("2026-10-05", "1直", "山田", [clean_shift_rows("1直", "A")], first=True)
        # 2直: 記号がマスタに無い停止を残して(自動保存だけ)帰る
        body = self.next_shift("2026-10-05 15:05", "佐藤")
        self.take_over("佐藤")
        bad = clean_shift_rows("2直", "B")
        bad["1"].update(S="Z", TH="5")
        self.at("2026-10-05 22:00")
        self.autosave(bad, "佐藤")
        self.work("2026-10-05", "3直", "夜勤", [clean_shift_rows("3直", "C")])
        self.shared_online()
        self.next_shift("2026-10-06 07:05", "鈴木")
        self.take_over("鈴木")
        res = self.push_past(worker="鈴木")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["succeeded"], 2)
        self.assertEqual(body["sent_shifts"], ["2026年10月5日 L-1 1直", "2026年10月5日 L-1 3直"])
        self.assertEqual([r["shift"] for r in body["held"]], ["2直"])
        self.assertIn("過去分: 送った直 2件 / エラーで残した直 1件", body["message"])
        self.assertIn("■ 2026年10月5日 L-1 2直", body["message"])
        self.assertIn("停止内訳にない記号です", body["message"])
        self.assertEqual(body["sound_cue"], "", "残した直があるのに済んだ音")
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "1直", 1), ("2026年10月5日", "3直", 1)})
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "2直", 1)})
        self.assertEqual({r["直"] for r in self.shared_rows("T_日報集計_L-1")}, {"1直", "3直"})
        self.assertEqual([x["shift"] for x in self.state(worker="鈴木")["handover"]["left"]], ["2直"])
        # 06:55 の3直の自動確定は終わる3直だけを見る(2直に止められない)。同じ1分に
        # 拾った2直(確定し忘れ)は過去分の道で、2直だけ止めて残る。手で押した1回も同じ
        self.assertEqual([(h["shift"], h["result"], h["pressed_by"], h["via"]) for h in self.history()
                          if h["result"] == "関門で止めた"],
                         [("2直", "関門で止めた", "(自動確定)", "次の直の画面が拾って送った"),
                          ("2直", "関門で止めた", "鈴木", "過去分")])
        # 管理者が2直を直せば、同じボタンで残りが届く
        self.admin_on()
        self.recall("2026年10月5日", "2直")
        self.assertEqual(self.save(clean_shift_rows("2直", "B"), "佐藤").status_code, 200)
        self.back()
        res = self.push_past(worker="鈴木")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["succeeded"], 1)
        self.assertIn("過去分: 送った直 1件 / エラーで残した直 0件", res.get_json()["message"])
        self.assertEqual(self.dirty_keys(), set())
        self.assertFalse(self.state(worker="鈴木")["handover"]["any_left"])

    def test_D2_溜まった過去分5直のうち2直がエラー_3直送れて2直残り_直せば同じボタンで送れる(self) -> None:
        """【v4.35.0】「前の直を送る は溜まったエラー過去直全部送れると思っていいですよね?」
        ── 「過去分を共有へ送る」は、いまの直ではない未送信の直ぜんぶ(何日前でも)を1回で
        古い順に送る。エラーのある直は送らずに帯に残し、直したあと同じボタンで送れる。"""
        self.shared_offline()
        self.work("2026-10-05", "1直", "山田", [clean_shift_rows("1直", "A")], first=True)
        # 10/5 2直: 停止の記号がマスタに無い(自動保存だけ)
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.take_over("佐藤")
        bad2 = clean_shift_rows("2直", "B")
        bad2["1"].update(S="Z", TH="5")
        self.at("2026-10-05 22:00")
        self.autosave(bad2, "佐藤")
        self.work("2026-10-05", "3直", "夜勤", [clean_shift_rows("3直", "C")])
        # 10/6 1直: 梱包数が検入枚数を超えている(自動保存だけ)
        self.next_shift("2026-10-06 07:05", "鈴木")
        self.take_over("鈴木")
        bad1 = clean_shift_rows("1直", "E")
        bad1["1"].update(KEN="5", MAI="10", TUT="1")
        self.at("2026-10-06 14:00")
        self.autosave(bad1, "鈴木")
        self.work("2026-10-06", "2直", "高橋", [clean_shift_rows("2直", "F")])
        # 10/7 の朝に共有へ届くようになった。いまの直(10/7 1直)はまだ何も打っていない
        self.shared_online()
        body = self.next_shift("2026-10-07 07:05", "田中")
        self.assertEqual(len(body["handover"]["left"]), 5)
        self.assertIn("直すところの無い3直は「過去分を共有へ送る」で送れます", body["handover"]["message"])
        res = self.push_past(worker="田中")
        self.assertEqual(res.status_code, 200, res.get_json())
        got = res.get_json()
        self.assertEqual(got["sent_shifts"], ["2026年10月5日 L-1 1直", "2026年10月5日 L-1 3直",
                                              "2026年10月6日 L-1 2直"])
        self.assertEqual([(r["report_date"], r["shift"]) for r in got["held"]],
                         [("2026年10月5日", "2直"), ("2026年10月6日", "1直")])
        self.assertIn("過去分: 送った直 3件 / エラーで残した直 2件", got["message"])
        self.assertIn("■ 2026年10月5日 L-1 2直", got["message"])
        self.assertIn("停止内訳にない記号です", got["message"])
        self.assertIn("■ 2026年10月6日 L-1 1直", got["message"])
        self.assertIn("梱包数", got["message"])
        self.assertIn("同じ「過去分を共有へ送る」でもう一度押すと送れます", got["message"])
        self.assertEqual(self.shared_keys(), {("2026年10月5日", "1直", 1), ("2026年10月5日", "3直", 1),
                                              ("2026年10月6日", "2直", 1)})
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "2直", 1), ("2026年10月6日", "1直", 1)})
        band = self.state(worker="田中")["handover"]
        self.assertEqual([(x["report_date"], x["shift"]) for x in band["left"]],
                         [("2026年10月5日", "2直"), ("2026年10月6日", "1直")])
        # 管理者が2直ぶんを直して、同じボタンをもう一度
        self.admin_on()
        self.recall("2026年10月5日", "2直")
        self.assertEqual(self.save(clean_shift_rows("2直", "B"), "佐藤").status_code, 200)
        self.recall("2026年10月6日", "1直")
        self.assertEqual(self.save(clean_shift_rows("1直", "E"), "鈴木").status_code, 200)
        self.back()
        res = self.push_past(worker="田中")
        self.assertEqual(res.status_code, 200, res.get_json())
        got = res.get_json()
        self.assertEqual(got["sent_shifts"], ["2026年10月5日 L-1 2直", "2026年10月6日 L-1 1直"])
        self.assertEqual(got["held"], [])
        self.assertIn("過去分: 送った直 2件 / エラーで残した直 0件", got["message"])
        self.assertEqual(self.dirty_keys(), set())
        self.assertEqual(len(self.shared_keys()), 5)
        self.assertFalse(self.state(worker="田中")["handover"]["any_left"])
        self.assertEqual(self.shared_lots()[("2026年10月5日", "2直", 1)], ["B01", "B02"])

    def three_shifts(self) -> None:
        """共有に届かないまま 1直(1ページ)・2直(2ページ)・3直(1ページ) を溜める。"""
        self.shared_offline()
        self.work("2026-10-05", "1直", "山田", [clean_shift_rows("1直", "A")], first=True)
        self.work("2026-10-05", "2直", "佐藤", second_pages("B"))
        self.work("2026-10-05", "3直", "夜勤", [clean_shift_rows("3直", "C")])
        self.shared_online()
        self.next_shift("2026-10-06 07:05", "鈴木")
        self.take_over("鈴木")

    def test_D3_途中で失敗したら送れたぶんだけ印が付き_送り直しで二重にならない(self) -> None:
        self.three_shifts()
        from nippou.access_bridge import sqlite_backend
        real = sqlite_backend._write_records

        def locked_on_2nd_page(conn, header, *args, **kwargs):
            if (header.shift, header.page) == ("2直", 2):
                raise sqlite3.OperationalError("database is locked")
            return real(conn, header, *args, **kwargs)

        with patch.object(sqlite_backend, "_write_records", side_effect=locked_on_2nd_page):
            res = self.push_past(worker="鈴木")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual((body["succeeded"], body["failed"]), (3, 1))
        self.assertIn("失敗: 1件", body["message"])
        self.assertEqual(body["sound_cue"], "")
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "2直", 2)})
        self.assertIn(("2026年10月5日", "2直", 1), self.shared_keys())
        self.assertNotIn(("2026年10月5日", "2直", 2), self.shared_keys())
        results = {h["shift"]: (h["result"], h["failed_pages"]) for h in self.history()
                   if h["result"] != "直せないまま引き継いだ"}
        self.assertEqual(results, {"1直": ("送れた", 0), "2直": ("一部送れなかった", 1),
                                   "3直": ("送れた", 0)})
        # 断りの文は「ロック中」と読める(中身の分からない文にならない)
        self.assertIn("database is locked", body["message"])
        self.assertNotIn("has no attribute", body["message"])
        # 帯は2直が残る
        self.assertEqual([x["shift"] for x in self.state(worker="鈴木")["handover"]["left"]], ["2直"])
        # 送り直し
        res = self.push_past(worker="鈴木")
        self.assertEqual(res.get_json()["succeeded"], 1)
        self.assertEqual(self.dirty_keys(), set())
        self.assertEqual(len(self.shared_headers()), 4)
        self.assertEqual(len(self.shared_details()), 4 * 12)
        self.assertEqual(self.shared_lots()[("2026年10月5日", "2直", 2)], ["BP201", "BP202"])

    def test_D3_共有のフォルダに届かない_書けない(self) -> None:
        self.three_shifts()
        self.shared_offline()
        res = self.push_past(worker="鈴木")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.get_json()["succeeded"], res.get_json()["failed"]), (0, 4))
        self.assertEqual(len(self.dirty_keys()), 4)
        self.assertEqual({h["result"] for h in self.history() if h["result"] != "直せないまま引き継いだ"},
                         {"送れなかった"})
        self.assertFalse((self.tmp / "shared_off" / "日報データ.sqlite3").exists())
        # 手元は壊れていない
        self.assertEqual(self.local_lots()[("2026年10月5日", "2直", 2)], ["BP201", "BP202"])
        self.shared_online()
        with patch("nippou.access_bridge.sqlite_backend._write_records",
                   side_effect=sqlite3.OperationalError("attempt to write a readonly database")):
            res = self.push_past(worker="鈴木")
        self.assertEqual(res.get_json()["failed"], 4)
        self.assertEqual(len(self.dirty_keys()), 4)
        self.assertEqual(self.push_past(worker="鈴木").get_json()["succeeded"], 4)

    def test_D3_共有のファイルだけ無いときは新しく作る(self) -> None:
        """【注意】フォルダがあってファイルが無いと、空のファイルを作って書く(届いたことになる)。"""
        self.three_shifts()
        self.assertFalse(self.shared_path.exists())
        res = self.push_past(worker="鈴木")
        self.assertEqual(res.get_json()["succeeded"], 4)
        self.assertTrue(self.shared_path.exists())

    def test_D4_送った直を直して送り直すと置き換わる(self) -> None:
        self.three_shifts()
        self.push_past(worker="鈴木")
        self.assertEqual(len(self.shared_lots()[("2026年10月5日", "2直", 1)]), 12)
        self.admin_on()
        # 1ページ目の11・12行目を消す
        self.recall("2026年10月5日", "2直", 1)
        page1 = second_pages("B")[0]
        del page1["11"], page1["12"]
        page1["1"]["LOT"] = "BX01"
        self.assertEqual(self.save(page1, "佐藤").status_code, 200)
        # 2ページ目を空にする(ページそのものは消せない ── 共有へ渡したので)
        self.recall("2026年10月5日", "2直", 2)
        self.assertEqual(self.save({}, "佐藤").status_code, 200)
        refused = self.post("/api/settings/empty-pages/delete",
                            {"report_date": "2026年10月5日", "line": "L-1", "shift": "2直", "page": 2})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("共有へ渡してある", refused.get_json()["message"])
        self.back()
        self.assertEqual(self.dirty_keys(), {("2026年10月5日", "2直", 1), ("2026年10月5日", "2直", 2)})
        res = self.push_past(worker="鈴木", skip=SKIP)
        self.assertEqual(res.status_code, 200, res.get_json())
        lots = self.shared_lots()
        self.assertEqual(lots[("2026年10月5日", "2直", 1)][:2], ["BX01", "BP102"])
        self.assertEqual(len(lots[("2026年10月5日", "2直", 1)]), 10)
        self.assertEqual(lots[("2026年10月5日", "2直", 2)], [], "空にしたページに古い行が残る")
        self.assertIn(("2026年10月5日", "2直", 2), self.shared_keys(), "ページの見出しは残る")
        self.assertEqual(len(self.shared_details()), 4 * 12, "行が増えた")
        summary = [r for r in self.shared_rows("T_日報集計_L-1") if r["直"] == "2直"]
        self.assertEqual(float(summary[0]["枚数"]), 100.0)

    def test_D5_ほかの端末が送った行を消さない(self) -> None:
        from nippou.access_bridge import sqlite_backend
        from nippou.db.models import DetailRecord, HeaderRecord

        def other(line, day, shift, lot):
            key = dict(report_date=day, line=line, shift=shift, page=1)
            sqlite_backend.push_records(
                self.shared_path, HeaderRecord(**key, worker="別の人"),
                [DetailRecord(**key, row_no=1, lot=lot)],
                f"T_日報ヘッダー_{line}", f"T_日報明細_{line}")

        other("LVC", "2026年10月5日", "1直", "LVC01")          # 別のライン
        other("L-1", "2026年10月1日", "1直", "OLD01")          # 同じライン・別の直(別のPC)
        other("L-1", "2026年10月5日", "1直", "SAME01")         # 同じ鍵(同じ紙)
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.assertEqual(self.push_now(worker="山田").status_code, 200)
        self.assertEqual(self.shared_lots("LVC"), {("2026年10月5日", "1直", 1): ["LVC01"]})
        lots = self.shared_lots()
        self.assertEqual(lots[("2026年10月1日", "1直", 1)], ["OLD01"])
        # 同じ鍵は「同じ紙」なので置き換わる(後から送ったほうが残る)
        self.assertEqual(lots[("2026年10月5日", "1直", 1)], ["A01", "A02"])

    def test_D6_月をまたいで溜めて送ると前の月を書き出す(self) -> None:
        self.shared_offline()
        self.work("2026-09-30", "2直", "佐藤", [clean_shift_rows("2直", "S")], first=True)
        self.work("2026-09-30", "3直", "夜勤", [clean_shift_rows("3直", "N")])
        body = self.next_shift("2026-10-01 07:05", "山田")
        self.take_over("山田")
        self.at("2026-10-01 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.shared_online()
        # 過去分(9月の2直・3直)を送ると、9月に残りが無くなるので書き出す
        res = self.push_past(worker="山田")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["succeeded"], 2)
        months = [r["decision"]["label"] for r in body["rollovers"]]
        self.assertEqual(months, ["2026年9月"])
        folder = self.tmp / "monthly" / "L-1" / "2026.09"
        self.assertTrue(any(folder.glob("*.csv")), "9月を書き出していない")
        self.assertFalse((self.tmp / "monthly" / "L-1" / "2026.10").exists())
        self.assertIn((2026, 9), self.repo().settled_months())
        # いまの1直はふだんのボタンで。9月は済んでいるので書き出し直さない
        now = self.push_now(worker="山田").get_json()
        self.assertEqual(now["succeeded"], 1)
        self.assertEqual(now["rollovers"], [])
        # 9月の直をあとから直して送ると、9月を書き出し直す
        self.admin_on()
        self.recall("2026年9月30日", "2直")
        fixed = clean_shift_rows("2直", "S")
        fixed["1"]["MAI"] = "30"
        self.save(fixed, "佐藤")
        self.back()
        again = self.push_past(worker="山田").get_json()
        self.assertEqual([r["decision"]["label"] for r in again["rollovers"]], ["2026年9月"])

    def test_D6_前の月の直が止まっていると月の書き出しもしない(self) -> None:
        self.start_shift("2026-09-30 15:05", "佐藤")
        bad = clean_shift_rows("2直", "S")
        bad["1"].update(KEN="1")
        self.at("2026-09-30 22:00")
        self.autosave(bad, "佐藤")
        self.next_shift("2026-10-01 07:05", "山田")
        self.take_over("山田")
        res = self.push_past(worker="山田")
        self.assertEqual(res.status_code, 422)
        self.assertFalse((self.tmp / "monthly").exists())
        self.assertNotIn((2026, 9), self.repo().settled_months())

    def test_D6_いまの直だけ送れても_前の月の残りがあれば月を書き出さない(self) -> None:
        """【v4.35.0】ふだんの「共有へ保存」は前の月の直が止まっていても いまの直を送れる。
        そのときも、前の月にまだ共有へ出ていない直があるうちは月を書き出さない(見送ったと言う)。"""
        self.start_shift("2026-09-30 15:05", "佐藤")
        bad = clean_shift_rows("2直", "S")
        bad["1"].update(KEN="1")
        self.at("2026-09-30 22:00")
        self.autosave(bad, "佐藤")
        self.next_shift("2026-10-01 07:05", "山田")
        self.take_over("山田")
        self.at("2026-10-01 14:30")
        self.save(clean_shift_rows("1直", "A"))
        res = self.push_now(worker="山田")
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["succeeded"], 1)
        self.assertEqual(self.shared_keys(), {("2026年10月1日", "1直", 1)})
        self.assertEqual([(r["decision"]["label"], r["held"], r["ran"]) for r in body["rollovers"]],
                         [("2026年9月", True, False)])
        self.assertIn("2026年9月 の書き出しは見送りました", body["message"])
        self.assertFalse((self.tmp / "monthly").exists())
        self.assertNotIn((2026, 9), self.repo().settled_months())
        # 管理者が9月の2直を直して過去分を送れば、9月を書き出す
        self.admin_on()
        self.recall("2026年9月30日", "2直")
        self.assertEqual(self.save(clean_shift_rows("2直", "S"), "佐藤").status_code, 200)
        self.back()
        body = self.push_past(worker="山田").get_json()
        self.assertEqual([(r["decision"]["label"], r["held"]) for r in body["rollovers"]],
                         [("2026年9月", False)])
        self.assertTrue(any((self.tmp / "monthly" / "L-1" / "2026.09").glob("*.csv")))
        self.assertIn((2026, 9), self.repo().settled_months())

    def test_D_ページを出したまま空の最終ページでも最終時間を見る(self) -> None:
        """次ページ発行で空の2ページ目ができたまま、1ページ目は 9:50 で終わっている。
        最終時間(15:00)まで無いのに、「直すところはありません」で共有へ出ていた。"""
        self.start_shift("2026-10-05 07:05", "山田")
        items = [row(f"T{n:02d}", f"{7 + (n - 1) // 6:02d}:{((n - 1) % 6) * 10:02d}",
                     f"{7 + n // 6:02d}:{(n % 6) * 10:02d}") for n in range(1, 12)]
        items.append(rest("08:50", "09:50"))
        self.at("2026-10-05 10:00")
        self.assertEqual(self.new_page(rows_of(*items)).status_code, 200)
        checked = self.post("/api/entry/verify", {}).get_json()
        self.assertIn("shift_end", [f["code"] for f in checked["findings"]], checked["message"])
        self.assertEqual(self.push_now().status_code, 422)


# ======================================================================
# E. 共有が Access(.accdb)のとき
# ======================================================================
class E_AccessTests(ScenarioCase):
    """この環境では本物の Access は動かない。台本を走らせる口(`ScriptRunner.run`)を
    差し替えて、どの経路を通るか・失敗しても手元が壊れないかを見る。"""

    def setUp(self) -> None:
        super().setUp()
        from nippou import config, user_settings
        user_settings.save(config.KEY_ACCESS_DB_FILE, "日報データ.accdb")
        self.scripts: list[str] = []
        # ロックの待ち直し(1秒×回数)は待たない
        from dataclasses import replace
        from nippou.access_bridge import runner
        waiting = patch.object(runner, "SETTINGS", replace(runner.SETTINGS, retry_base_wait_sec=0.0))
        waiting.start()
        self.addCleanup(waiting.stop)

    def runner(self, *, ok: bool = True, desc: str = ""):
        from nippou.access_bridge.runner import ScriptResult

        def run(_self, script_text: str):
            self.scripts.append(script_text)
            if "ALTER TABLE" in script_text:
                return ScriptResult(success=True)
            return ScriptResult(success=ok, err_desc=desc, err_number=None if ok else 3260,
                                stdout="OK" if ok else f"ERRCODE=3260|ERRDESC={desc}")
        return patch("nippou.access_bridge.runner.ScriptRunner.run", run)

    def two_shifts(self) -> None:
        self.shared_offline()
        self.start_shift("2026-10-05 07:05", "山田")
        self.at("2026-10-05 14:30")
        self.save(clean_shift_rows("1直", "A"))
        self.close_tick("2026-10-05 14:55")
        self.next_shift("2026-10-05 15:05", "佐藤")
        self.take_over("佐藤")
        self.at("2026-10-05 22:30")
        self.save(clean_shift_rows("2直", "B"), "佐藤")
        self.shared_online()

    def test_E1_書き先はAccessの経路を通る(self) -> None:
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.access_db_path.suffix, ".accdb")
        self.two_shifts()
        with self.runner(ok=True):
            res = self.push(worker="佐藤")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["succeeded"], 2)
        pushes = [s for s in self.scripts if "ALTER TABLE" not in s]
        self.assertEqual(len(pushes), 2, "1ページ1台本(1キー1トランザクション)")
        joined = "\n".join(pushes)
        self.assertIn("DELETE FROM [T_日報ヘッダー_L-1] WHERE [報告日]='2026年10月5日'", joined)
        self.assertIn("INSERT INTO [T_日報明細_L-1]", joined)
        self.assertIn("'A01'", joined)
        self.assertIn("'B02'", joined)
        self.assertEqual(self.dirty_keys(), set())
        # sqlite3 のファイルは作らない・集計の表も履歴の表もAccessへは写さない
        self.assertFalse(self.shared_path.exists())
        self.assertNotIn("T_日報集計", joined)
        self.assertNotIn("T_共有保存履歴", joined)
        # 手で押したぶんは送れた。14:55 の自動確定(共有に届かない)は送れなかったで残る(v4.34.2)
        self.assertEqual({h["result"] for h in self.history()
                          if h["result"] != "直せないまま引き継いだ" and h["pressed_by"] != "(自動確定)"},
                         {"送れた"})
        self.assertEqual([h["result"] for h in self.history() if h["pressed_by"] == "(自動確定)"],
                         ["送れなかった"])
        self.assertTrue(all(not h["shared"] for h in self.history()))
        # 【注意】集計(packing_report)は Access へは送らないので、手元で未送信のまま
        self.assertTrue(self.packing("2026年10月5日", "1直").dirty)

    def test_E2_Accessに書けないとき手元は壊れない(self) -> None:
        self.two_shifts()
        before = self.local_lots()
        with self.runner(ok=False, desc="ロックされています"):
            res = self.push(worker="佐藤")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.get_json()["succeeded"], res.get_json()["failed"]), (0, 2))
        self.assertEqual(len(self.dirty_keys()), 2)
        self.assertEqual(self.local_lots(), before)
        self.assertEqual({h["result"] for h in self.history() if h["result"] != "直せないまま引き継いだ"},
                         {"送れなかった"})
        # 月替わりは送れなかった回には走らない
        self.assertEqual(res.get_json()["rollovers"], [])
        # 直ったら送れる
        with self.runner(ok=True):
            self.assertEqual(self.push(worker="佐藤").get_json()["succeeded"], 2)
        self.assertEqual(self.dirty_keys(), set())

    def test_E2_cscriptが無い環境でも落ちない(self) -> None:
        """差し替えない本物の口。Windows でなければ cscript.exe が無く、失敗として返る。"""
        self.two_shifts()
        before = self.local_lots()
        res = self.push(worker="佐藤")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["failed"], 2)
        self.assertEqual(self.local_lots(), before)
        self.assertEqual(len(self.dirty_keys()), 2)

    def test_E3_誰かが開いていると知らせる(self) -> None:
        self.two_shifts()
        (self.shared_dir / "日報データ.laccdb").write_text("x", encoding="utf-8")
        with self.runner(ok=True):
            res = self.push(worker="佐藤")
        self.assertTrue(res.get_json()["warning"])
        self.assertIn(res.get_json()["warning"], res.get_json()["message"])

    def test_E4_同じ名前のsqlite3があればそちらへ書く(self) -> None:
        sqlite3.connect(self.shared_path).close()
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.access_db_path, self.shared_path)
        self.two_shifts()
        with self.runner(ok=True):
            res = self.push(worker="佐藤")
        self.assertEqual(res.get_json()["succeeded"], 2)
        self.assertEqual(self.scripts, [])
        self.assertEqual(len(self.shared_keys()), 2)
