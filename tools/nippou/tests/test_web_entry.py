"""日報入力画面(Web版)のテスト

**業務判断がサーバ側にあること**を主に確かめる。単重計算・排他制御・
LOT重複・時刻の引き継ぎは、いずれも `logic/` の純関数が答えを出して
いて、画面はそれを写すだけ ── その境界が守られているかを見る。
"""
from __future__ import annotations

import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import NO_TOKEN_HEADERS, WebTestCase


class EntryPageTests(WebTestCase):
    def test_画面が出る(self) -> None:
        res = self.get("/")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("日報入力", body)
        # 12行ぶんの入力欄が描かれていること
        self.assertIn('id="LOT1"', body)
        self.assertIn('id="LOT12"', body)
        self.assertNotIn('id="LOT13"', body)

    def test_レールに5画面が出る(self) -> None:
        body = self.get("/").get_data(as_text=True)
        for label in ("日報入力", "梱包資材重量計算", "集計・グラフ", "印刷", "設定・管理者"):
            self.assertIn(label, body)

    def test_版が帯に出る(self) -> None:
        # 「その端末に入っているのはどれか」を画面から読めること
        body = self.get("/").get_data(as_text=True)
        self.assertIn("VER", body)


class UnitWeightTests(WebTestCase):
    """単重計算(`単重計算` = 重量 / 包み数)がサーバ側で決まること。"""

    def test_単重がサーバで計算される(self) -> None:
        res = self.post("/api/entry/state", {
            "rows": {"1": {"LOT": "L-1", "CON": "10", "WEI": "100"}},
            "header": {}, "checks": {},
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["rows"]["1"]["UNI"], "10.00")

    def test_LOTが空なら単重は出さない(self) -> None:
        # VBA `単重計算` の分岐をそのまま引き継いでいること
        res = self.post("/api/entry/state", {
            "rows": {"1": {"LOT": "", "CON": "10", "WEI": "100"}},
            "header": {}, "checks": {},
        })
        self.assertEqual(res.get_json()["rows"]["1"]["UNI"], "")

    def test_知らない行番号は無視する(self) -> None:
        # 送られた値は誰にでも決められるので、想定外は捨てる
        res = self.post("/api/entry/state", {
            "rows": {"99": {"LOT": "X"}, "0": {"LOT": "Y"}},
            "header": {}, "checks": {},
        })
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("99", res.get_json()["rows"])


class ExclusionTests(WebTestCase):
    """チェックボックスの排他制御(`HdCh_排他制御`)。"""

    def test_HdCh1を入れると関係するものが外れる(self) -> None:
        res = self.post("/api/entry/check", {
            "rows": {}, "header": {},
            "checks": {"HdCh1": True, "HdCh2": True, "HdCh3": True},
            "name": "HdCh1",
        })
        checks = res.get_json()["checks"]
        self.assertTrue(checks["HdCh1"])
        self.assertFalse(checks["HdCh2"])
        self.assertFalse(checks["HdCh3"])

    def test_知らない名前は400(self) -> None:
        res = self.post("/api/entry/check",
                        {"rows": {}, "header": {}, "checks": {}, "name": "NoSuch"})
        self.assertEqual(res.status_code, 400)


class SaveTests(WebTestCase):
    def test_保存して読み直せる(self) -> None:
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A", "WEI": "10"}},
            "header": {"worker": "山田"}, "checks": {},
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["message"], "保存しました")

        # 画面を開き直しても残っていること
        body = self.get("/").get_data(as_text=True)
        self.assertIn("山田", body)

    def test_LOT重複は422で断り画面ぜんぶを返す(self) -> None:
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A"}, "2": {"LOT": "A"}},
            "header": {}, "checks": {},
        })
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertIn("重複", body["message"])
        self.assertEqual(body["duplicates"], ["A"])
        # **断られた画面が古いままにならない**よう、画面ぜんぶも返す
        self.assertIn("rows", body)
        self.assertIn("header", body)

    def test_重複していれば保存されない(self) -> None:
        self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A"}, "2": {"LOT": "A"}},
            "header": {"worker": "重複太郎"}, "checks": {},
        })
        self.assertEqual(self.repo().list_keys(), [])

    def test_silentなら文言を出さない(self) -> None:
        # 自動保存は操作していない場面で走るので、いちいち知らせない
        res = self.post("/api/entry/save", {
            "rows": {"1": {"LOT": "A"}}, "header": {}, "checks": {},
            "silent": True,
        })
        self.assertEqual(res.get_json()["message"], "")


class LineTests(WebTestCase):
    """この端末のライン。**据え付けのときに1度決めるもの。**

    日報入力の1番目に並んでいると「まず選ぶもの」に見えて、押し間違えた
    まま打ち始められる ── ラインが違えば保存先のキーごと変わるので、
    気づくのは翌日の集計。設定(管理者モード)へ移した。
    """

    def admin(self) -> None:
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})

    def test_管理者モードでなければ変えられない(self) -> None:
        res = self.post("/api/entry/line", {"line": "LVC"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("管理者", res.get_json()["error"]["message"])

    def test_ラインを変えられる(self) -> None:
        self.admin()
        res = self.post("/api/entry/line", {"line": "LVC"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["line"], "LVC")

    def test_知らないラインは400(self) -> None:
        # **知らない名前は、権限を見る前に断る**(打ち間違いの話なので)
        res = self.post("/api/entry/line", {"line": "NOPE"})
        self.assertEqual(res.status_code, 400)

    def test_丸徳以外に切り替えると設備番号が消える(self) -> None:
        self.admin()
        self.post("/api/entry/line", {"line": "中板", "maru_sub": "3"})
        res = self.post("/api/entry/line", {"line": "L-1"})
        # 帯に嘘(存在しない設備番号)を出さない
        self.assertEqual(res.get_json()["maru_sub"], "")

    def test_丸徳の設備番号は1から7だけ(self) -> None:
        self.admin()
        res = self.post("/api/entry/line", {"line": "中板", "maru_sub": "99"})
        self.assertEqual(res.get_json()["maru_sub"], "")

    def test_日報入力にライン選択は無い(self) -> None:
        html = self.get("/").get_data(as_text=True)
        self.assertNotIn('id="lines"', html)

    def test_設定にライン選択がある(self) -> None:
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="line-setting"', html)
        self.assertIn('id="line-choices"', html)


class SecurityTests(WebTestCase):
    def test_トークンが無ければ401(self) -> None:
        res = self.client.post("/api/entry/save", json={},
                               headers=NO_TOKEN_HEADERS)
        self.assertEqual(res.status_code, 401)

    def test_healthはトークン無しで通る(self) -> None:
        # 多重起動の判定と起動待機画面が、トークンを知らないまま尋ねる
        res = self.client.get("/api/health", headers=NO_TOKEN_HEADERS)
        self.assertEqual(res.status_code, 200)

    def test_Hostが違えば400(self) -> None:
        res = self.client.get("/api/health", headers={"Host": "evil.example.com"})
        self.assertEqual(res.status_code, 400)

    def test_別オリジンからの要求は403(self) -> None:
        res = self.client.post("/api/entry/save", json={}, headers={
            "Host": "127.0.0.1", "X-Tool-Token": "test-token",
            "Sec-Fetch-Site": "cross-site",
        })
        self.assertEqual(res.status_code, 403)

    def test_非ASCIIのトークンでも500にしない(self) -> None:
        # `compare_digest` に str を渡すと TypeError で500になる
        res = self.client.post("/api/entry/save", json={}, headers={
            "Host": "127.0.0.1", "X-Tool-Token": "にせトークン",
        })
        self.assertEqual(res.status_code, 401)

    def test_画面のHTMLは控えさせない(self) -> None:
        # 「入れ替えたのに古いまま」を防ぐ
        res = self.get("/")
        self.assertIn("no-store", res.headers["Cache-Control"])


class BootGateTests(WebTestCase):
    """準備が終わるまでは起動待機画面を出す (基盤仕様書 2.3)。"""

    ready = False

    def test_準備中は待機画面が出る(self) -> None:
        body = self.get("/").get_data(as_text=True)
        self.assertIn("起動しています", body)
        self.assertNotIn('id="LOT1"', body)

    def test_準備中でもhealthは答える(self) -> None:
        res = self.client.get("/api/health", headers=NO_TOKEN_HEADERS)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["ready"])

    def test_準備中でも静的ファイルは通る(self) -> None:
        # 待機画面自身が読むので、止めると何も出なくなる
        res = self.client.get("/static/css/tokens.css", headers=NO_TOKEN_HEADERS)
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
