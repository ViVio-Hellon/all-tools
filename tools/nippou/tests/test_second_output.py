"""集計CSVの2つ目の出力先 ── **共有へ保存したときに出す**

    集計データの出力先を２つ設定したいです(使用、アクセス権の関係)
    月別の書き出し(自動集計)も2つ目に … あります
    ２つ目に追加した出力先に出すタイミングは共有保存を押したタイミングに

2つ目は見る人とアクセス権の違う所です。そこへ出すのは**共有へ送った
日報**だけにします(`services/second_output`)。

    共有へ保存で送れたとき      … 送った日の4本(1つ目と同じ形)
    月替わりの書き出し          … `<2つ目>/自動集計/<ライン>/yyyy.mm` に同じ3本
    取り込み(そのままにする)    … 入れた日の4本(共有に入っている扱いなので)
    保存(確定)・グラフ・集計管理 … **1つ目だけ**(2つ目には出さない)
    2つ目に届かない             … 共有への保存は成功のまま、知らせに載せる
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import HAS_FLASK, SKIP_REASON, WebTestCase  # noqa: E402
from tests.test_auto_export import payload  # noqa: E402

# 日のフォルダの3本と、年月のフォルダの式の説明(`計算内容.csv`。v4.2.0)
NAMES = sorted(["集計", "集計明細", "停止内訳", "計算内容.csv"])


def csv_names(base: Path) -> list[str]:
    return sorted(p.name.split("_")[0] for p in base.rglob("*.csv")) if base.is_dir() else []


@unittest.skipUnless(HAS_FLASK, SKIP_REASON)
class SecondOutputTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.tmp / "現場の共有"
        self.second = self.tmp / "管理だけ"
        from nippou import config, user_settings
        user_settings.save(config.KEY_REPORT_OUT_DIR, str(self.first))
        user_settings.save(config.KEY_MONTHLY_DIR, str(self.tmp / "月別"))

    def use_second(self, path: Path) -> None:
        from nippou import config, user_settings
        user_settings.save(config.KEY_REPORT_OUT_DIR2, str(path))

    def saved_key(self):
        """保存した直のページの鍵 `(日, ライン, 直, ページ)`。"""
        body = self.post("/api/entry/save", payload()).get_json()
        self.assertTrue(body["saved"], body)
        keys = [h.key() for h in self.repo().pending_sync_headers()]
        self.assertTrue(keys)
        return keys[0]

    def push(self, keys, rolls=None):
        """共有へ保存。**送れた鍵を決めて**押す(書き先と関門は差し替える)。

        本物と同じく、送れたページには送れた印を付けます(残りは未送信のまま)。
        `rolls` は月替わりの結果 (1つ目, 2つ目) の並び(既定は月替わり無し)。
        """
        from nippou.access_bridge import pusher

        def fake_push(repo, _path, *args, **kwargs):
            for key in keys:
                repo.mark_synced(key)
            return pusher.PushSummary(succeeded=list(keys))
        with patch("nippou.services.shift_check.run_pending", return_value=[]), \
                patch("nippou.access_bridge.pusher.push_pending", side_effect=fake_push), \
                patch("app.routes.settings._rollover_after_push",
                      return_value=list(rolls or [])):
            return self.post("/api/settings/push", {})

    def two_shifts(self, day="2026年9月24日"):
        """同じ日の1直と2直を保存しておく(どちらも未送信)。鍵を返す。"""
        from nippou.db.models import DetailRecord, HeaderRecord
        keys = []
        for shift in ("1直", "2直"):
            key = dict(report_date=day, line="L-1", shift=shift, page=1)
            self.repo().save(HeaderRecord(**key, worker="山田"),
                             [DetailRecord(**key, row_no=1, lot="A1")])
            keys.append((day, "L-1", shift, 1))
        return keys

    # -- 決め方 --------------------------------------------------------
    def test_決めていなければ1つ目だけ(self) -> None:
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.summary_csv_dirs, [self.first])
        self.assertIsNone(SETTINGS.report_output_dir_2)

    def test_1つ目と同じ道なら1つにまとめる(self) -> None:
        from nippou.config import SETTINGS
        self.use_second(self.first)
        self.assertEqual(SETTINGS.summary_csv_dirs, [self.first])

    def test_変えるにはパスワードが要る(self) -> None:
        res = self.post("/api/settings/paths",
                        {"report_output_dir_2": str(self.second)})
        self.assertEqual(res.status_code, 403)
        res = self.post("/api/settings/paths",
                        {"report_output_dir_2": str(self.second), "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_json())
        from nippou.config import SETTINGS
        self.assertEqual(SETTINGS.summary_csv_dirs, [self.first, self.second])

    # -- 出すとき ------------------------------------------------------
    def test_保存確定では2つ目に出さない(self) -> None:
        """共有にまだ無い中身を、先に2つ目へ届けない。"""
        self.use_second(self.second)
        body = self.post("/api/entry/save", payload()).get_json()
        self.assertTrue(body["export"]["ok"], body["export"])
        self.assertEqual(csv_names(self.first), NAMES)
        self.assertEqual(csv_names(self.second), [])
        self.assertNotIn("2つ目", body["export"]["message"])

    def test_共有へ保存で送れた日を2つ目へ(self) -> None:
        """**ここが本題。** 共有保存を押したときに出す。"""
        self.use_second(self.second)
        key = self.saved_key()
        body = self.push([key]).get_json()
        self.assertEqual(csv_names(self.second), NAMES)
        self.assertIn("2つ目の出力先にも送った日の集計CSVを出しました", body["message"])
        self.assertTrue(body["second_output"]["daily"]["copies"][0]["ok"])
        # 1つ目と同じ形(ライン/集計/年月/日)
        one = sorted(p.relative_to(self.first) for p in self.first.rglob("*.csv"))
        two = sorted(p.relative_to(self.second) for p in self.second.rglob("*.csv"))
        self.assertEqual(one, two)

    def test_送れなかったぶんは出さない(self) -> None:
        self.use_second(self.second)
        self.saved_key()
        body = self.push([]).get_json()
        self.assertEqual(csv_names(self.second), [])
        self.assertEqual(body["second_output"]["daily"]["copies"], [])

    def test_送れなかった直がある日は出さずに待つ(self) -> None:
        """**ここが本題(2)。** 日ごとのCSVに、まだ共有に無い直を入れない。

            同じ日に、まだ共有へ送っていない直が別にあると、その直のぶんも
            2つ目に入ります … 直してください
        """
        self.use_second(self.second)
        first, second = self.two_shifts()
        body = self.push([first]).get_json()             # 2直だけ送れなかった
        self.assertEqual(csv_names(self.second), [])
        self.assertIn("まだ送れていない直がある", body["message"])
        self.assertIn("2026年9月24日 L-1", body["message"])
        self.assertEqual(body["second_output"]["daily"]["held"], ["2026年9月24日 L-1"])
        # 次の共有へ保存で2直が送れたら、そこで出る
        body = self.push([second]).get_json()
        self.assertEqual(csv_names(self.second), NAMES)
        self.assertIn("2つ目の出力先にも送った日の集計CSVを出しました", body["message"])
        self.assertEqual(body["second_output"]["daily"]["held"], [])

    def test_ほかの日の未送信は待つ理由にならない(self) -> None:
        from nippou.db.models import DetailRecord, HeaderRecord
        self.use_second(self.second)
        keys = []
        for day in ("2026年9月24日", "2026年9月25日"):
            key = dict(report_date=day, line="L-1", shift="1直", page=1)
            self.repo().save(HeaderRecord(**key, worker="山田"),
                             [DetailRecord(**key, row_no=1, lot="A1")])
            keys.append((day, "L-1", "1直", 1))
        body = self.push([keys[0]]).get_json()           # 24日だけ送った
        self.assertEqual(body["second_output"]["daily"]["held"], [])
        names = [p.name for p in self.second.rglob("*.csv")]
        self.assertTrue(any("09-24" in n for n in names), names)
        self.assertFalse(any("09-25" in n for n in names), names)

    def test_決めていなければ2つ目のことは言わない(self) -> None:
        key = self.saved_key()
        body = self.push([key]).get_json()
        self.assertNotIn("2つ目", body["message"])

    def test_2つ目に届かなくても共有への保存は成功(self) -> None:
        """2つ目はアクセス権の違う所。**届かない日は普通にある。**"""
        blocked = self.tmp / "ふさがっている"
        blocked.write_text("ファイルなのでフォルダを作れない", encoding="utf-8")
        self.use_second(blocked / "下")
        key = self.saved_key()
        res = self.push([key])
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["succeeded"], 1)
        self.assertIn("出せませんでした", body["message"])
        self.assertFalse(body["second_output"]["daily"]["copies"][0]["ok"])

    def test_グラフと集計管理のボタンは1つ目だけ(self) -> None:
        self.use_second(self.second)
        self.post("/api/entry/save", payload())
        self.post("/api/graph/csv", {})
        res = self.post("/api/agg/csv", {"start": "2020-01-01", "end": "2099-12-31"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertFalse(list(self.second.rglob("*.csv")))

    def test_印刷用HTMLは出さない(self) -> None:
        self.use_second(self.second)
        key = self.saved_key()
        self.push([key])
        self.assertFalse(list(self.second.rglob("*.html")))

    # -- 月別の書き出し ------------------------------------------------
    def test_月別の書き出しも2つ目の自動集計へ(self) -> None:
        from nippou.services import month_rollover, second_output
        from nippou.logic.shift import parse_business_date
        from nippou.config import SETTINGS
        self.use_second(self.second)
        key = self.saved_key()
        self.repo().mark_synced(key)                      # 共有へ送り終わった月
        day = parse_business_date(key[0])
        result = month_rollover.run(self.repo(), SETTINGS.monthly_dir,
                                    month=(day.year, day.month))
        self.assertTrue(result.ran)
        outcome = second_output.after_rollover(self.repo(), result)
        self.assertTrue(outcome.copies and outcome.copies[0].ok, outcome)
        self.assertEqual(outcome.held, [])
        monthly = self.second / "自動集計" / "L-1" / f"{day.year}.{day.month:02d}"
        self.assertTrue(monthly.is_dir(), list(self.second.rglob("*")))
        self.assertEqual(len(list(monthly.glob("*.csv"))), len(result.exports[0].files))

    def test_月別もその月に未送信があれば待つ(self) -> None:
        from nippou.services import month_rollover, second_output
        from nippou.config import SETTINGS
        self.use_second(self.second)
        self.two_shifts()                                 # どちらも未送信
        result = month_rollover.run(self.repo(), SETTINGS.monthly_dir, month=(2026, 9))
        outcome = second_output.after_rollover(self.repo(), result)
        self.assertEqual(outcome.copies, [])
        self.assertEqual(outcome.held, ["2026年9月のぶん"])
        self.assertFalse((self.second / "自動集計").exists())

    def test_月替わりを押して走らせても2つ目へ(self) -> None:
        from nippou.logic.shift import parse_business_date
        self.use_second(self.second)
        key = self.saved_key()
        self.repo().mark_synced(key)
        day = parse_business_date(key[0])
        res = self.post("/api/settings/rollover", {"month": f"{day.year}-{day.month:02d}"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue((self.second / "自動集計" / "L-1").is_dir())
        self.assertIn("2つ目の出力先にも月別の書き出しを出しました", res.get_json()["message"])

    def push_next_month(self, keys):
        """共有へ保存を、**保存した月の翌月に**押す(月替わりは本物を走らせる)。"""
        from datetime import date

        from nippou.access_bridge import pusher
        from nippou.logic.month_roll import next_month
        from nippou.logic.shift import parse_business_date

        day = parse_business_date(keys[0][0])
        today = date(*next_month(day.year, day.month), 1)

        def fake_push(repo, _path, *args, **kwargs):
            for key in keys:
                repo.mark_synced(key)
            return pusher.PushSummary(succeeded=list(keys))
        with patch("nippou.services.shift_check.run_pending", return_value=[]), \
                patch("nippou.access_bridge.pusher.push_pending", side_effect=fake_push), \
                patch("nippou.services.month_rollover.date") as clock:
            clock.today.return_value = today
            return self.post("/api/settings/push", {})

    def test_共有へ保存の直後の月替わりも2つ目へ(self) -> None:
        """月替わりは共有へ保存の直後に走る。そのときも2つ目へ出す。"""
        self.use_second(self.second)
        key = self.saved_key()
        body = self.push_next_month([key]).get_json()
        self.assertIn("月別の書き出しを出しました", body["message"])
        self.assertTrue((self.second / "自動集計" / "L-1").is_dir())
        self.assertEqual(len(body["rollovers"]), 1)

    def test_書き出した月は次の共有へ保存で出し直さない(self) -> None:
        """**覚えていなかった頃は、翌月の保存のたびに同じ月を出し直していた。**"""
        self.use_second(self.second)
        key = self.saved_key()
        first = self.push_next_month([key]).get_json()
        self.assertEqual(len(first["rollovers"]), 1)
        again = self.push_next_month([key]).get_json()
        self.assertEqual(again["rollovers"], [], again["message"])
        self.assertNotIn("を書き出しました", again["message"])

    def test_2つ目で待った月は次の共有へ保存でもう1度(self) -> None:
        """未送信が残って2つ目を待った月は、**済んだことにしない。**"""
        from nippou.services import month_rollover
        self.use_second(self.second)
        keys = self.two_shifts("2026年8月24日")
        # 1直だけ送れた → 8月に未送信が残る → 2つ目は待つ
        body = self.push_next_month(keys[:1]).get_json()
        self.assertIn("まだ送れていない直がある", body["message"])
        self.assertNotIn((2026, 8), self.repo().settled_months())
        # 残りが送れた → こんどは出して、済んだことにする
        body = self.push_next_month(keys[1:]).get_json()
        self.assertIn("月別の書き出しを出しました", body["message"])
        self.assertIn((2026, 8), self.repo().settled_months())
        self.assertTrue(month_rollover.MAX_MONTHS_PER_RUN >= 1)

    # -- 取り込み ------------------------------------------------------
    def test_取り込みはそのままにするときだけ2つ目へ(self) -> None:
        """「そのままにする」は共有に入っている扱い。「共有にも入れる」は
        あとで共有へ保存を押したときに出る。"""
        from unittest.mock import MagicMock

        from nippou.services import csv_import
        self.use_second(self.second)
        found = MagicMock(ok=True)
        found.parsed.pages = []
        found.targets = []
        for mark_synced, expected in ((True, 1), (False, 0)):
            with self.subTest(mark_synced=mark_synced), \
                    patch("nippou.services.csv_import.preview", return_value=found), \
                    patch("nippou.services.csv_import._write_copies") as copies:
                csv_import.apply(self.repo(), self.tmp / "x.csv", mark_synced=mark_synced)
            self.assertEqual(copies.call_count, expected)
            if expected:
                self.assertEqual(copies.call_args.args[3], [self.second])

    def test_取り込みで2つ目に出せなければ結果に載せる(self) -> None:
        from nippou.services import csv_import
        result = csv_import.Result()
        with patch("nippou.reporting.csv_export.write_daily_set",
                   side_effect=OSError("届きません")):
            csv_import._write_copies(self.repo(), result,
                                     [("2026年9月1日", "L-1")], [self.second])
        self.assertEqual(len(result.csv_failed), 1)
        self.assertIn("2つ目", result.csv_failed[0][0])

    # -- 画面 ----------------------------------------------------------
    def test_参照設定に2つ目の欄がある(self) -> None:
        html = self.get("/settings?tab=paths").get_data(as_text=True)
        self.assertIn("集計CSVの出力パス(2つ目)", html)
        self.assertIn("「共有へ保存」で送れたとき", html)
        self.assertIn("使っていません", html)        # 空のうちは探しに行かない
        self.assertIn('placeholder="空なら出しません"', html)

    def test_決めると出力先の行に2つ目と出す時が出る(self) -> None:
        self.use_second(self.second)
        html = self.get("/graph").get_data(as_text=True)
        self.assertIn("集計CSVの2つ目", html)
        self.assertIn(str(self.second), html)
        self.assertIn("このボタンでは出ません", html)
        html = self.get("/settings?tab=paths").get_data(as_text=True)
        self.assertNotIn("使っていません", html)

    def test_配布設定にも入れられる(self) -> None:
        from nippou import distribution
        self.assertIn("report_output_dir_2", distribution.ITEM_KEYS)


if __name__ == "__main__":
    unittest.main()
