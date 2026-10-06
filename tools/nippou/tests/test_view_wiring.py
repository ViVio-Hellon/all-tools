"""画面ごとのモジュールが、2度目に来ても繋がるか

【何を守っているのか】
`nav.js` は画面を移るとき `<main>` を差し替えます。**ES モジュールは
一度読んだら二度と実行されない**ので、モジュールのトップレベルで
`addEventListener` していると、差し替えで作り直された要素には
何も付きません ── つまり **2 度目にその画面へ来ると、ボタンが全部
死にます。**

実際にそうなっていました。「管理者モードにする」が効かない・
「参照パスの保存ができません」・「入力制限が何も効いてない」は、
どれも同じこれ1つが原因です。

そこで約束を決めました:

    ・画面ごとのモジュールは `export function start()` を持つ
    ・配線はぜんぶ `start()` の中(要素は毎回引き直す)
    ・テンプレートは `src=` ではなく `data-view=` で道を書く
      (`nav.mountViews()` が `import()` して `start()` を呼ぶ)

この約束はブラウザを動かさないと壊れたことが分からない ── 壊れても
Python のテストは全部通ります。だから**字面で見張ります。**
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "app" / "static" / "js" / "views"
TEMPLATES = ROOT / "app" / "templates"


class ViewModuleTests(unittest.TestCase):
    """画面ごとのモジュールの約束。"""

    def test_どの画面もstartを出している(self) -> None:
        missing = [p.name for p in sorted(VIEWS.glob("*.js"))
                   if "export function start" not in p.read_text(encoding="utf-8")]
        self.assertEqual(missing, [], f"start() が無い: {missing}")

    def test_トップレベルで配線していない(self) -> None:
        """`document.getElementById(...)` を module の直下で呼ばない。

        字下げの無い行だけを見ます。`start()` の中は必ず字下げされて
        いるので、**字下げ 0 で `document.` から始まる行があれば、
        それは一度きりしか動かない配線**です。
        """
        bad: list[str] = []
        for path in sorted(VIEWS.glob("*.js")):
            for no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                if line.startswith(("document.", "window.")):
                    bad.append(f"{path.name}:{no}: {line.strip()[:60]}")
        self.assertEqual(bad, [], "トップレベルで配線しています:\n" + "\n".join(bad))

    def test_テンプレートはdata_viewで読ませる(self) -> None:
        """`<script type="module" src=...views/...>` を残さない。

        `src` で入れ直しても中身は動きません(モジュールは一度きり)。
        `data-view` なら `nav.mountViews()` が `start()` を呼びます。
        """
        pattern = re.compile(r'<script[^>]*\bsrc=[^>]*js/views/')
        bad = [p.name for p in sorted(TEMPLATES.glob("*.html"))
               if pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], f"src= で読ませている: {bad}")

    def test_テンプレートのdata_viewは実在する(self) -> None:
        pattern = re.compile(r"data-view=\"\{\{ url_for\('static', "
                             r"filename='(js/views/[a-z_]+\.js)'\) \}\}\"")
        found = 0
        for path in sorted(TEMPLATES.glob("*.html")):
            for name in pattern.findall(path.read_text(encoding="utf-8")):
                found += 1
                self.assertTrue((ROOT / "app" / "static" / name).is_file(),
                                f"{path.name} が無いファイルを指しています: {name}")
        # 1つも見つからないなら、書き方が変わっている(この見張りが空回りする)
        self.assertGreater(found, 0, "data-view の書き方が変わっています")

    def test_入口を_import_しない(self) -> None:
        """`app.js` は `<script src>` から1回だけ読まれる入口。

        views が `../app.js` を import していたせいで、`<script>` が読む
        版付きの `app.js?v=…` と、相対 import が読む版なしの `app.js` が
        **別のモジュールとして2回評価され**、トップレベルの副作用が全部
        2度走っていました:

            ・nav.start() が2回 → document のクリック listener が2つ
            ・nav.mountViews() が2回 → **どのボタンにも listener が2つ**
            ・心拍と音の見張りも2つずつ

        `api` / `toast` は本来の置き場所から取ります。
        """
        bad = []
        for path in sorted(VIEWS.glob("*.js")) + [
                ROOT / "app" / "static" / "js" / f
                for f in ("api.js", "toast.js", "nav.js", "sound.js",
                          "health.js", "chart.js", "busy.js")]:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r'^\s*import[^;]*from\s+["\'][^"\']*app\.js["\']',
                         text, re.M):
                bad.append(path.name)
        self.assertEqual(bad, [], f"app.js を import しています: {bad}")

    def test_入口は再輸出しない(self) -> None:
        """再輸出があると、また import したくなる。"""
        text = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"^export\s*\{", "app.js が再輸出しています")

    def test_全部のviewがどこかから読まれている(self) -> None:
        """置いてあるのに誰も読まないモジュールを作らない。"""
        wired = set()
        for path in TEMPLATES.glob("*.html"):
            wired.update(re.findall(r"filename='js/(views/[a-z_]+\.js)'",
                                    path.read_text(encoding="utf-8")))
        here = {f"views/{p.name}" for p in VIEWS.glob("*.js")}
        self.assertEqual(here - wired, set(),
                         f"どのテンプレートからも読まれていません: {here - wired}")


class NavTests(unittest.TestCase):
    """差し替えの側の約束。"""

    def setUp(self) -> None:
        self.nav = (ROOT / "app" / "static" / "js" / "nav.js").read_text(
            encoding="utf-8")
        self.app = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")

    def test_差し替えのあとにstartを呼ぶ(self) -> None:
        self.assertIn("export async function mountViews", self.nav)
        self.assertIn("mod.start()", self.nav)
        # 差し替えの流れ(`go`)からも呼ばれること
        self.assertIn("  mountViews();", self.nav)

    def test_最初の1回も同じ道を通る(self) -> None:
        """初回だけ効く/初回だけ効かない、を作らない。"""
        self.assertIn("nav.mountViews()", self.app)

    def test_起動の見張りが素のスクリプトで入っている(self) -> None:
        """`type="module"` は**読めなかったとき何も言わずに止まります。**

        404・構文の誤り・import の失敗のどれでも、画面はふつうに出たまま
        どのボタンも効きません ── 押す人からは「効いていない」としか
        見えず、手がかりが画面のどこにもありませんでした。実際に
        入れ替えのあと全画面で起きています。

        見張りは **module ではない**素のスクリプトに置きます。module が
        転んでも動く場所でないと、見張りごと死にます。
        """
        base = (ROOT / "app" / "templates" / "base.html").read_text(
            encoding="utf-8")
        head, _, tail = base.partition('<script type="module"')
        self.assertIn("APP.booted", head, "見張りが module より後にあります")
        self.assertIn("bootProblem", head)
        # module 側は、繋ぎ終わったら見張りを解く
        app = (ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("booted = true", app)

    def test_JSが死んだら面を全部開く(self) -> None:
        """面を切り替えるのは JS。転んだままだと開いていない面へ
        **二度と辿り着けません**(版の印を出す面も含めて)。"""
        base = (ROOT / "app" / "templates" / "base.html").read_text(
            encoding="utf-8")
        nav = (ROOT / "app" / "static" / "js" / "nav.js").read_text(
            encoding="utf-8")
        for name, text in (("base.html", base), ("nav.js", nav)):
            with self.subTest(where=name):
                self.assertIn(".tabpanel[hidden]", text)

    def test_画面の中でlocation_reloadしない(self) -> None:
        """`location.reload()` は外枠ごと作り直す。

        出したばかりのトーストも接続断の帯も消えるので、
        差し替えで取り直す(`nav.refresh()`)。
        """
        bad = [p.name for p in sorted(VIEWS.glob("*.js"))
               if "location.reload(" in p.read_text(encoding="utf-8")]
        self.assertEqual(bad, [], f"location.reload() を使っています: {bad}")


if __name__ == "__main__":
    unittest.main()
