"""日報のページを書く・消すのは services/page_writer の 1 か所(安全網)

画面の外から書いた(消した)ページは、開いたままの古い画面に上書きさせない印を付ける。
以前は全停入力だけが印を付け、CSV の取り込み・直の付け替え・空のページの片付けは付け忘れていた
(古い画面の自動保存が、取り込んだ行を前の中身で戻す・消したページを作り直す)。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nippou.db.models import HeaderRecord  # noqa: E402


def live():
    """いま読み込まれている page_writer(試験の土台がモジュールを読み直すことがある)。"""
    from nippou.services import page_writer

    return page_writer


class FakeRepo:
    def __init__(self) -> None:
        self.saved, self.deleted = [], []

    def save(self, header, details):
        self.saved.append(header.key if hasattr(header, "key") else header)

    def delete_page(self, *key):
        self.deleted.append(key)
        return True

    def saved_pages(self, day, line, shift):
        return [1, 2]

    def mark_synced(self, key):
        pass


class PageWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        live().reset()
        self.addCleanup(live().reset)

    def test_画面の外から書いたら印を付け_画面から書いたら付けない(self) -> None:
        repo = FakeRepo()
        live().save_page(repo, HeaderRecord("2026/10/09", "LVC", "1直", 1), [], by_screen=True)
        self.assertIsNone(live().written_aside_at(("2026/10/09", "LVC", "1直", 1)))
        live().save_page(repo, HeaderRecord("2026/10/09", "LVC", "1直", 2), [], by_screen=False)
        self.assertIsNotNone(live().written_aside_at(("2026/10/09", "LVC", "1直", 2)))

    def test_消したページにも印を付ける(self) -> None:
        live().delete_page(FakeRepo(), "2026/10/09", "LVC", "1直", 3)
        self.assertIsNotNone(live().written_aside_at(("2026/10/09", "LVC", "1直", 3)))

    def test_直の付け替えは消したページと書いたページの両方に印を付ける(self) -> None:
        from nippou.services import reshift

        header = HeaderRecord("2026/10/09", "LVC", "3直", 1)
        reshift._rewrite(FakeRepo(), "2026/10/09", "LVC", "3直", header, [], synced=False)
        for page in (1, 2):
            self.assertIsNotNone(live().written_aside_at(("2026/10/09", "LVC", "3直", page)), page)

    def test_ページを書く処理はpage_writerを通す(self) -> None:
        """**repo.save / delete_page を直に呼ぶ経路を足さない。** 足すと印の付け忘れが戻る。"""
        allowed = {ROOT / "nippou" / "services" / "page_writer.py", ROOT / "nippou" / "db" / "repository.py"}
        found = []
        for path in list((ROOT / "nippou").rglob("*.py")) + list((ROOT / "app").rglob("*.py")):
            if path in allowed or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for no, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"(repo|get_repo\(\))\.(save|delete_page)\(", code):
                    found.append(f"{path.relative_to(ROOT)}:{no}: {line.strip()}")
        self.assertEqual(found, [], "page_writer を通さずにページを書いている")


if __name__ == "__main__":
    unittest.main()
