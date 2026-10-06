"""CSVの出力先を設定で決められること、そして**押す前に見えること**

【なぜこのテストがあるか】
「CSVってどこに出力してます？わかりにくい」と言われたのがもとです。
出したあとの知らせにはパスが入っていましたが、知らせは消えるので、
**押す前**にはどこへ出るのか分かりませんでした。分からないまま押すと、
探しに行くところから始まります。

ここで見るのは2つ:
    1. 設定で出力先を決められる(決めたらそこへ本当に出る)
    2. 出力先が、押す前のボタンの隣に**常に**出ている
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._web import WebTestCase


class OutputDirViewTests(WebTestCase):
    """`presenters/settings.output_dir_view()` が出すもの。"""

    def _view(self) -> dict:
        from nippou.presenters import settings as settings_view

        return settings_view.output_dir_view()

    def test_既定でも実際のパスを出す(self) -> None:
        """「既定です」では探しに行けない。**道そのもの**を出す。"""
        view = self._view()
        self.assertTrue(view["is_default"])
        self.assertEqual(view["value"], "")
        self.assertTrue(view["path"])
        # 既定はこの端末の中(NIPPOU_APP_DIR を向いている)
        self.assertIn(str(self.tmp), view["path"])

    def test_設定したらそこを出す(self) -> None:
        from nippou import user_settings

        out = self.tmp / "共有" / "集計"
        user_settings.save_many({"report_output_dir": str(out)})
        view = self._view()
        self.assertEqual(view["path"], str(out))
        self.assertFalse(view["is_default"])
        self.assertEqual(view["value"], str(out))

    def test_まだ無くても困りごとにしない(self) -> None:
        """出力先は**書き出すときに作られる**。無いのは普通の状態。"""
        from nippou import user_settings
        from nippou.presenters import settings as settings_view

        user_settings.save_many({"report_output_dir": str(self.tmp / "まだ無い")})
        self.assertFalse(self._view()["exists"])
        # 参照パスの一覧でも、赤い印を出さない側に入れてある
        self.assertIn("report_output_dir", settings_view.OPTIONAL_PATH_KEYS)

    def test_管理者パスワードで守る(self) -> None:
        """**パスは全部守ります**(「パスの変更は全部パスワードを必須に」)。

        出力先は書く側なので、はじめは守っていませんでした。ですが
        書き先を間違えると**出したはずのものが誰にも見えません** ──
        出た先を確かめる人は普段いないので、気づくのは「先月ぶんが
        無い」と言われたときです。
        """
        from nippou.presenters import settings as settings_view

        self.assertIn("report_output_dir", settings_view.PROTECTED_LABELS)
        res = self.post("/api/settings/paths",
                        {"report_output_dir": str(self.tmp / "共有")})
        self.assertEqual(res.status_code, 403)
        res = self.post("/api/settings/paths",
                        {"report_output_dir": str(self.tmp / "共有"),
                         "password": "nisk"})
        self.assertEqual(res.status_code, 200)


class OutputDirScreenTests(WebTestCase):
    """押す前に見えるか。"""

    def test_グラフ画面に出力先が出る(self) -> None:
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn("出力先:", body)
        self.assertIn("設定・参照設定で変える", body)

    def test_集計管理の画面にも出る(self) -> None:
        body = self.get("/agg").get_data(as_text=True)
        self.assertIn("出力先:", body)

    def test_設定したパスが画面に出る(self) -> None:
        from nippou import user_settings

        out = self.tmp / "共有" / "集計CSV"
        user_settings.save_many({"report_output_dir": str(out)})
        self.assertIn(str(out), self.get("/graph").get_data(as_text=True))
        self.assertIn(str(out), self.get("/agg").get_data(as_text=True))

    def test_既定のときは端末の中だと言い添える(self) -> None:
        """共有だと思って出すと、誰も開けないまま終わる。"""
        body = self.get("/graph").get_data(as_text=True)
        self.assertIn("この端末の中", body)

    def test_設定してあれば言い添えない(self) -> None:
        from nippou import user_settings

        user_settings.save_many({"report_output_dir": str(self.tmp / "共有")})
        self.assertNotIn("この端末の中", self.get("/graph").get_data(as_text=True))

    def test_設定画面に出力先の欄がある(self) -> None:
        body = self.get("/settings").get_data(as_text=True)
        self.assertIn("集計CSV・印刷用HTMLの出力パス", body)


class OutputDirWriteTests(WebTestCase):
    """決めた場所へ**本当に出る**か。"""

    def test_グラフのCSVは設定した場所へ出る(self) -> None:
        from nippou import user_settings

        out = self.tmp / "共有" / "集計"
        user_settings.save_many({"report_output_dir": str(out)})

        body = self.post("/api/graph/csv", {}).get_json()
        self.assertTrue(body["path"].startswith(str(out)))
        self.assertTrue(body["detail_path"].startswith(str(out)))
        self.assertTrue(Path(body["path"]).is_file())
        self.assertTrue(Path(body["detail_path"]).is_file())
        # 知らせにも道が入る(出したあとに確かめる先)
        self.assertIn(str(out), body["message"])

    def test_集計管理のCSVも設定した場所へ出る(self) -> None:
        from nippou import user_settings

        out = self.tmp / "共有" / "集計"
        user_settings.save_many({"report_output_dir": str(out)})

        res = self.post("/api/agg/csv",
                        {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(len(body["files"]), 4)
        for text in body["files"]:
            self.assertIn(str(out), text)

    def test_設定を変えれば次から新しい場所へ出る(self) -> None:
        """**変えたのに前のところに出続ける**が一番たちが悪い。"""
        from nippou import user_settings

        first = self.tmp / "まえ"
        user_settings.save_many({"report_output_dir": str(first)})
        self.post("/api/graph/csv", {})

        after = self.tmp / "あと"
        user_settings.save_many({"report_output_dir": str(after)})
        body = self.post("/api/graph/csv", {}).get_json()
        self.assertTrue(body["path"].startswith(str(after)))


if __name__ == "__main__":
    unittest.main()
