"""本物のブラウザ(Chromium + Playwright)を立てる。試験 4 本で共通。

無ければその試験は飛ばす。ただし **CI では飛ばさない**: 環境変数 ``ALLTOOLS_REQUIRE_BROWSER=1``
のときは、立てられなければ失敗にする(飛ばしたまま緑になると、画面の試験が一度も
動いていないことに誰も気づかない。実際に CI では一度も動いていなかった)。
"""
from __future__ import annotations

import glob
import os
import unittest


def chromium():
    """``(playwright, browser)``。立てられなければ ``(None, None)``。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    pw = sync_playwright().start()
    for path in [None] + sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")):
        try:
            return pw, (pw.chromium.launch(executable_path=path) if path
                        else pw.chromium.launch())
        except Exception:                          # noqa: BLE001 - 次を試す
            continue
    pw.stop()
    return None, None


def chromium_or_skip():
    """立てる。無ければ飛ばす(``ALLTOOLS_REQUIRE_BROWSER=1`` なら失敗にする)。"""
    pw, browser = chromium()
    if browser is None:
        if os.environ.get("ALLTOOLS_REQUIRE_BROWSER") == "1":
            raise RuntimeError("Chromium(Playwright)を立てられません。CI では画面の試験を飛ばしません")
        raise unittest.SkipTest("Chromium(Playwright)がありません")
    return pw, browser
