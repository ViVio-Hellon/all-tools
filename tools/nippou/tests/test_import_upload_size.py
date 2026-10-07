"""取り込み(ファイルを渡す): 欠けて届いたファイルは読まずに、そう言う

デスクトップ版の窓(WebView2)は、FormData に入れたファイルの中身を渡さないことがあった
(看板の「Access の最新で表の中身を入れ替える」・python-web-tools の「表を持ってくる」で
空のファイルが届いた)。画面は渡したファイルの大きさを添え、届いた大きさが違えばその1本を
読まずに「欠けて届きました」と言う ── 読めば「読めませんでした」になり、ファイルが悪いように見える。
"""
from __future__ import annotations

import io
import json
import unittest

from tests._web import HEADERS, WebTestCase


class ImportUploadSizeTests(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        res = self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

    def upload(self, files, sizes):
        data = {"files": [(io.BytesIO(body), name) for name, body in files],
                "line": "L-1", "dry_run": "1"}
        if sizes is not None:
            data["sizes"] = json.dumps(sizes)
        return self.client.post("/api/settings/import/upload", data=data, headers=HEADERS,
                                content_type="multipart/form-data")

    def test_欠けて届いた1本だけ読まずにそう言う(self) -> None:
        good = "作業日,ライン\n".encode("utf-8")
        res = self.upload([("欠けた.csv", b""), ("届いた.csv", good)], [1234, len(good)])
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        files = {f["name"]: f for f in res.get_json()["files"]}
        self.assertFalse(files["欠けた.csv"]["ok"])
        self.assertIn("欠けて届きました(0 / 1,234 バイト)", files["欠けた.csv"]["error"])
        self.assertNotIn("欠けて", files["届いた.csv"].get("error") or "", "大きさの合う1本まで断った")

    def test_大きさが添えられていなければ今までどおり読む(self) -> None:
        res = self.upload([("古い画面.csv", b"")], None)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertNotIn("欠けて", res.get_json()["files"][0].get("error") or "")


if __name__ == "__main__":
    unittest.main()
