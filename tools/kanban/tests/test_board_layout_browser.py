"""看板の盤の見え方を**本物のブラウザで**確かめる(VER3.6.2 の現場の声)。

1. 押せないボタン(倉庫参照・参照だけのライン・発注の無い行の発送など)は、**薄くしない**。
   文字はふだんの濃さのまま(コントラスト比 4.5 以上)で、押せないことは破線の枠・斜線の地・
   禁止のカーソル・マウスを乗せたときの「参照だけ(押せません)」で示す。ライト・ダークとも
2. 盤は**極力スクロールなしで見える**ように並べる(段組みで縦に詰める・サイズは 1 行・
   横にはみ出さない)。1920×1080・1366×768・1280×1024 で見る
3. それでもはみ出すときは、**はみ出している向きに「▼ 下にまだあります(n 件)」**を出し、
   スクロールしきったら消す。数は本当に見えていない看板の数。横も同じ

画面写真は頼まれたときだけ撮る(環境変数 ``KANBAN_BOARD_SHOT`` に出力先のフォルダ)。

無い環境では飛ばします(CI では飛ばしません: ``ALLTOOLS_REQUIRE_BROWSER=1``)。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from kanban import config
from kanban.db.store import DEFAULT_COLUMN_MAP
from tests._kanban_browser import KanbanBrowserCase

#: 現場で見る大きさの画面
SCREENS = ((1920, 1080), (1366, 768), (1280, 1024))

#: ありそうな 1 ライン(資材 14・看板 46)。長いサイズ(「1030 × 1520 : 100枚」)と
#: 長い資材名(「ハードボード(大板棚)」)を含む
MATERIALS = (
    ("外装紙", ("2200 × 50M : 3本", "1900 × 50M : 5本", "1600 × 50M : 5本", "1300 × 50M : 5本")),
    ("アングル", ("2510 : 3束", "2010 : 3束", "1510 : 3束", "1010 : 5束")),
    ("ハードボード(大板棚)", ("1030 × 1520 : 100枚", "1000 × 1400 : 100枚", "660 × 1050 : 100枚",
                         "750 × 1130 : 100枚", "510 × 770 : 100枚")),
    ("ハードボード", ("900 × 1800 : 50枚", "600 × 900 : 50枚", "450 × 600 : 50枚")),
    ("段ボール", ("A-1 : 20枚", "A-2 : 20枚", "B-1 : 20枚", "B-2 : 20枚", "C-1 : 10枚", "C-2 : 10枚")),
    ("ストレッチフィルム", ("500 × 300M : 6本", "450 × 300M : 6本")),
    ("PPバンド", ("15.5mm : 2巻", "12mm : 2巻")),
    ("エアキャップ", ("1200 × 42M : 2本", "600 × 42M : 4本", "300 × 42M : 8本")),
    ("木枠材", ("2000 × 90 : 10本", "1800 × 90 : 10本", "1200 × 90 : 10本", "900 × 90 : 10本")),
    ("パレット", ("1100 × 1100 : 5枚", "1200 × 1000 : 5枚")),
    ("防錆紙", ("1000 × 100M : 2本", "500 × 100M : 2本")),
    ("乾燥剤", ("100g : 1箱", "50g : 1箱", "30g : 1箱")),
    ("ラベル", ("取扱注意 : 1冊", "天地無用 : 1冊", "精密機械 : 1冊")),
    ("テープ", ("布 50mm : 1箱", "OPP 48mm : 1箱")),
)


def board_rows(copies: int = 1) -> list[dict[str, str]]:
    """``copies`` 回くり返した看板(資材名は回ごとに変える)。4 行に 1 つ発注、8 行に 1 つ発送。"""
    out: list[dict[str, str]] = []
    for n in range(copies):
        for material, sizes in MATERIALS:
            name = material if n == 0 else f"{material}{n + 1}"
            for size in sizes:
                no = len(out) + 1
                want = "〇" if no % 4 == 1 else ""
                out.append({
                    "mgmt_no": str(no), "material": name, "size": size,
                    "want": want, "unwant": "" if want else "〇",
                    "ordered_at": "2026/07/14 09:05:00" if want else "",
                    "shipped": "〇" if no % 8 == 1 else "", "confirmed_at": "",
                    "permanent": "×" if "大板" in material else "〇",
                })
    return out


def import_line(store, line: str, rows: list[dict[str, str]]) -> None:
    store.import_line(line=line, table_name=f"看板_{line}", key_column="管理番号", key_category="TEXT",
                      column_map=dict(DEFAULT_COLUMN_MAP),
                      categories={name: "TEXT" for name in DEFAULT_COLUMN_MAP.values()},
                      rows=rows, source_path="dummy.accdb")


#: 押せない升・ボタンの見え方。文字と地(斜線の濃い所も)のコントラスト比を測る
DISABLED_LOOK = """(selector) => {
  const nums = (s) => (s.match(/[\\d.]+/g) || []).map(Number);
  const over = (fg, bg) => { const a = fg.length > 3 ? fg[3] : 1; return [0, 1, 2].map((i) => fg[i] * a + bg[i] * (1 - a)); };
  const lum = (c) => {
    const [r, g, b] = c.slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  // 地が透けている部品(地の色が透明)は、親の地の上に乗っている
  const solid = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = nums(getComputedStyle(e).backgroundColor);
      if (c.length === 3 || (c.length > 3 && c[3] > 0)) return over(c, [255, 255, 255]);
    }
    return [255, 255, 255];
  };
  const hatch = nums(getComputedStyle(document.documentElement).getPropertyValue('--st-disabled-hatch'));
  return [...document.querySelectorAll(selector)].filter((el) => el.disabled && el.offsetParent).map((el) => {
    const cs = getComputedStyle(el);
    let opacity = 1;
    for (let e = el; e; e = e.parentElement) opacity *= parseFloat(getComputedStyle(e).opacity);
    const bg = solid(el);
    const ink = over(nums(cs.color), bg);
    const after = getComputedStyle(el, '::after');
    return {
      text: el.textContent.trim(), kind: el.dataset.kind || el.id || '',
      opacity,
      veil: !['none', 'normal'].includes(after.content) && after.backgroundColor !== 'rgba(0, 0, 0, 0)',
      contrast: Math.min(ratio(ink, bg), ratio(ink, over(hatch, bg))),
      dashed: cs.borderTopStyle === 'dashed',
      hatched: cs.backgroundImage.includes('repeating-linear-gradient'),
      cursor: cs.cursor,
      title: el.title,
    };
  });
}"""


class DisabledLookTest(KanbanBrowserCase):
    """**押せないことは分かり、文字ははっきり読める**(倉庫参照)。"""

    MODE = config.MODE_WAREHOUSE_VIEW
    LINE = ""

    def setUp(self) -> None:
        super().setUp()
        import_line(self.store, "L1", board_rows())

    def themed(self, theme: str):
        page = self.watch(self.ctx.new_page())
        page.add_init_script(f"try {{ localStorage.setItem('kanban.theme_choice', '{theme}'); }} catch (e) {{}}")
        self.open("/board?line=L1", page)
        page.wait_for_selector('#board[data-line="L1"] .row')
        self.assertEqual(page.evaluate("document.documentElement.dataset.theme"), theme)
        return page

    def test_押せない升は薄くせず_破線と斜線と禁止のカーソルで示し_文字はコントラスト比45以上(self) -> None:
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page = self.themed(theme)
                cells = page.evaluate(DISABLED_LOOK, "#board .cell[data-kind]")
                kinds = {c["kind"] for c in cells}
                self.assertTrue({"size", "ship", "hold"} <= kinds, f"倉庫参照なのに押せる升がある: {kinds}")
                for c in cells:
                    where = f"{theme} {c['kind']}「{c['text']}」"
                    self.assertEqual(c["opacity"], 1, f"{where} が半透明(薄い)")
                    self.assertFalse(c["veil"], f"{where} に白い幕がかかっている")
                    self.assertGreaterEqual(c["contrast"], 4.5, f"{where} の文字が読みにくい")
                    self.assertTrue(c["dashed"], f"{where} の枠が破線でない")
                    self.assertTrue(c["hatched"], f"{where} に斜線の地が無い")
                    self.assertEqual(c["cursor"], "not-allowed", where)
                    self.assertIn("参照だけ(押せません)", c["title"], where)
                page.close()
        self.assertEqual(self.errors, [])

    def test_押せないボタンも薄くしない(self) -> None:
        """道具の並びのボタン(一括・更新など)も同じ見え方。押せないあいだも文字は読める。"""
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page = self.themed(theme)
                # 押せない普通のボタンと、押せない青いボタン(一括の形)を作って見る
                page.evaluate("""() => {
                  document.getElementById('refresh').disabled = true;
                  const b = document.createElement('button');
                  b.className = 'btn btn--primary'; b.id = 'probe'; b.disabled = true;
                  b.innerHTML = '表示中のラインを全て発送済みに <span class="count">0</span>';
                  document.querySelector('.toolbar').appendChild(b);
                }""")
                buttons = page.evaluate(DISABLED_LOOK, ".toolbar .btn")
                self.assertEqual({b["kind"] for b in buttons}, {"refresh", "probe"})
                for b in buttons:
                    where = f"{theme} {b['kind']}"
                    self.assertEqual(b["opacity"], 1, f"{where} が半透明(薄い)")
                    self.assertGreaterEqual(b["contrast"], 4.5, f"{where} の文字が読みにくい")
                    self.assertTrue(b["dashed"], where)
                    self.assertEqual(b["cursor"], "not-allowed", where)
                page.close()


class SiteDisabledLookTest(KanbanBrowserCase):
    """現場でも、押せない升(発注の無い行の発送など)は同じ見え方。説明は「押せません」。"""

    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def test_倉庫の押せない升も薄くしない(self) -> None:
        import_line(self.store, "L1", board_rows())
        page = self.open("/board?line=L1")
        page.wait_for_selector('#board[data-line="L1"] .row')
        cells = page.evaluate(DISABLED_LOOK, "#board .cell[data-kind]")
        self.assertTrue(cells, "試験の前提: 押せない升がある(倉庫のサイズ・発注の無い行の発送)")
        for c in cells:
            where = f"{c['kind']}「{c['text']}」"
            self.assertEqual(c["opacity"], 1, where)
            self.assertGreaterEqual(c["contrast"], 4.5, where)
            self.assertTrue(c["dashed"], where)
            self.assertIn("押せません", c["title"], where)
            self.assertNotIn("参照だけ", c["title"], f"{where}: 倉庫は参照だけではない")
        enabled = page.evaluate("[...document.querySelectorAll('#board .cell[data-kind]')]"
                                ".filter((b) => !b.disabled && b.dataset.kind !== 'comment')"
                                ".map((b) => getComputedStyle(b).borderTopStyle)")
        self.assertTrue(enabled, "試験の前提: 押せる升がある")
        self.assertNotIn("dashed", enabled, "押せる升まで破線になっている")
        self.assertEqual(self.errors, [])


#: 盤の並び。はみ出し・列の数・箱の高さ・サイズの行の数・印
LAYOUT = """() => {
  const root = document.scrollingElement;
  const frames = [...document.querySelectorAll('#board > .frame')].map((f) => f.getBoundingClientRect());
  const sizes = [...document.querySelectorAll('#board .cell--size')].map((el) => {
    const r = document.createRange(); r.selectNodeContents(el);
    return new Set([...r.getClientRects()].filter((x) => x.width > 0).map((x) => Math.round(x.top))).size;
  });
  const hint = document.getElementById('scroll-more');
  const right = document.getElementById('scroll-more-right');
  const board = document.getElementById('board').getBoundingClientRect();
  return {
    overY: root.scrollHeight - root.clientHeight, overX: root.scrollWidth - root.clientWidth,
    columns: new Set(frames.map((r) => Math.round(r.left))).size,
    frameSum: frames.reduce((s, r) => s + r.height, 0), frameMax: Math.max(...frames.map((r) => r.height)),
    boardHeight: board.height, count: frames.length,
    wrapped: sizes.filter((n) => n > 1).length,
    hint: !hint.hidden, hintText: hint.textContent, hintRect: hint.hidden ? null : hint.getBoundingClientRect().toJSON(),
    right: !right.hidden,
    vw: innerWidth, vh: innerHeight,
  };
}"""

#: 画面の下端より下に(一部でも)ある看板の数と、そのうち色付きのもの
BELOW = """() => {
  const rows = [...document.querySelectorAll('#board .row')].filter((r) => r.getBoundingClientRect().bottom > innerHeight + 1);
  return [rows.length, rows.filter((r) => r.querySelector('.cell[data-state]:not([data-state="neutral"])')).length];
}"""


class _Layout(KanbanBrowserCase):
    LINE = "L1"
    COPIES = 1

    def setUp(self) -> None:
        super().setUp()
        import_line(self.store, "L1", board_rows(self.COPIES))

    def at(self, width: int, height: int, path: str = "/board"):
        from kanban import screen

        # 画面の持ち主は 1 枚だけ。前に開いた画面を忘れさせる(残ると「この画面では
        # 操作できません」の覆いが出て押せない)
        screen.reset()
        page = self.watch(self.ctx.new_page())
        page.set_viewport_size({"width": width, "height": height})
        self.open(path, page)
        page.wait_for_selector("#board .row")
        self.settle(page)
        return page

    def shoot(self, name: str, path: str, sizes=((1920, 1080), (1366, 768)),
              themes=("light", "dark")) -> None:
        """画面写真(``KANBAN_BOARD_SHOT`` のフォルダへ)。"""
        from kanban import screen

        out = Path(os.environ["KANBAN_BOARD_SHOT"])
        out.mkdir(parents=True, exist_ok=True)
        for theme in themes:
            for width, height in sizes:
                screen.reset()
                page = self.watch(self.ctx.new_page())
                page.add_init_script(
                    f"try {{ localStorage.setItem('kanban.theme_choice', '{theme}'); }} catch (e) {{}}")
                page.set_viewport_size({"width": width, "height": height})
                self.open(path, page)
                page.wait_for_selector("#board .row")
                self.settle(page)
                page.wait_for_timeout(4000)    # 印の弾み(3 回)が止まるまで
                page.screenshot(path=str(out / f"board_{name}_{theme}_{width}x{height}.png"))
                page.close()

    @staticmethod
    def settle(page) -> None:
        """描き終えて、印を数え直すまで(印は次に描く前に数え直す)。"""
        page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")

    def layout(self, page) -> dict:
        return page.evaluate(LAYOUT)


class LayoutTest(_Layout):
    """**ありそうな 1 ライン(資材 14・看板 46)は、どの画面でも横にはみ出さず、サイズは 1 行。**

    1920×1080 ではスクロールなしで全部見える。箱は段組みで縦に詰める(格子のように、
    背の高い箱の横に空きを作らない)ので、盤の高さは「箱の高さの合計 ÷ 列の数」に近い。
    """

    MODE = config.MODE_SITE

    def test_どの画面でも横にはみ出さず_サイズは1行で_箱は縦に詰める(self) -> None:
        for width, height in SCREENS:
            with self.subTest(screen=f"{width}x{height}"):
                page = self.at(width, height)
                got = self.layout(page)
                self.assertLessEqual(got["overX"], 0, f"{width}x{height}: 横にはみ出す")
                self.assertEqual(got["wrapped"], 0, f"{width}x{height}: サイズが 2 行に割れる")
                self.assertGreaterEqual(got["columns"], 3, got)
                # 段組み: 列の高さがそろう。いちばん背の高い箱 1 つぶん以上の空きを作らない
                gap = 12 * got["count"]
                self.assertLessEqual(got["boardHeight"],
                                     (got["frameSum"] + gap) / got["columns"] + got["frameMax"], got)
                # はみ出していなければ印は出ない。はみ出していれば出る
                self.assertEqual(got["hint"], got["overY"] > 4, got)
                if (width, height) == (1920, 1080):
                    self.assertLessEqual(got["overY"], 4, f"1920x1080 でスクロールが要る: {got}")
                page.close()
        self.assertEqual(self.errors, [])

    def test_列の数は画面の幅で決まる(self) -> None:
        wide = self.layout(self.at(1920, 1080))["columns"]
        narrow = self.layout(self.at(1366, 768))["columns"]
        self.assertGreater(wide, narrow)
        self.assertGreaterEqual(narrow, 3)

    @unittest.skipUnless(os.environ.get("KANBAN_BOARD_SHOT"), "画面写真は頼まれたときだけ")
    def test_画面写真(self) -> None:
        self.shoot("site", "/board")


class WarehouseLayoutTest(LayoutTest):
    MODE = config.MODE_WAREHOUSE
    LINE = ""

    def at(self, width: int, height: int, path: str = "/board?line=L1"):
        page = super().at(width, height, path)
        page.wait_for_selector('#board[data-line="L1"] .row')
        self.settle(page)
        return page

    @unittest.skipUnless(os.environ.get("KANBAN_BOARD_SHOT"), "画面写真は頼まれたときだけ")
    def test_画面写真(self) -> None:
        self.shoot("warehouse", "/board?line=L1")


class ViewLayoutTest(WarehouseLayoutTest):
    """倉庫参照(どのボタンも押せない)でも同じ。"""

    MODE = config.MODE_WAREHOUSE_VIEW

    @unittest.skipUnless(os.environ.get("KANBAN_BOARD_SHOT"), "画面写真は頼まれたときだけ")
    def test_画面写真(self) -> None:
        self.shoot("view", "/board?line=L1")


class ScrollHintTest(_Layout):
    """**はみ出したら「▼ 下にまだあります(n 件)」を画面の下端に出し、スクロールしきったら消す。**

    資材 56・看板 184 のラインは、どの画面でも収まらない。印の数は本当に見えていない看板の数。
    """

    MODE = config.MODE_SITE
    COPIES = 4

    def test_下にはみ出すと印が出て_数は見えていない看板の数_下まで送ると消える(self) -> None:
        for width, height in SCREENS:
            with self.subTest(screen=f"{width}x{height}"):
                page = self.at(width, height)
                got = self.layout(page)
                self.assertGreater(got["overY"], 4, "試験の前提: 下にはみ出す")
                self.assertTrue(got["hint"], f"{width}x{height}: はみ出しているのに印が無い")
                below, lit = page.evaluate(BELOW)
                self.assertIn("▼ 下にまだあります", got["hintText"])
                self.assertIn(f"({below} 件", got["hintText"], "数が見えていない看板の数でない")
                self.assertIn(f"うち色付き {lit} 件", got["hintText"])
                # 画面の下端・左右の真ん中あたり
                rect = got["hintRect"]
                self.assertLessEqual(rect["bottom"], height)
                self.assertGreater(rect["bottom"], height - 40, rect)
                self.assertLess(abs((rect["left"] + rect["right"]) / 2 - width / 2), 10, rect)

                # 押すと下へ送り、数が減る
                page.click("#scroll-more")
                page.wait_for_function("scrollY > 0")
                page.wait_for_function("(n) => { const m = document.getElementById('scroll-more');"
                                       " return m.hidden || !m.textContent.includes('(' + n + ' 件'); }", arg=below)

                # 下までスクロールしきると消える
                page.evaluate("window.scrollTo(0, document.scrollingElement.scrollHeight)")
                page.wait_for_selector("#scroll-more", state="hidden")
                # 上へ戻るとまた出る
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_selector("#scroll-more", state="visible")
                page.close()
        self.assertEqual(self.errors, [])

    @unittest.skipUnless(os.environ.get("KANBAN_BOARD_SHOT"), "画面写真は頼まれたときだけ")
    def test_画面写真(self) -> None:
        self.shoot("many", "/board", sizes=((1366, 768),))

    def test_盤が描き直されて縮んだら印も消える(self) -> None:
        """看板が減って収まるようになったら(ほかの端末・マスタ管理)、印は消える。"""
        page = self.at(1366, 768)
        self.assertTrue(self.layout(page)["hint"])
        import_line(self.store, "L1", board_rows(1)[:6])
        page.click("#refresh")
        page.wait_for_function("document.querySelectorAll('#board .row').length === 6")
        page.wait_for_selector("#scroll-more", state="hidden")

    def test_横にはみ出すと右の印が出て_右まで送ると消える(self) -> None:
        page = self.at(1366, 768)
        self.assertFalse(self.layout(page)["right"], "横にはみ出していないのに右の印が出ている")
        # 横にはみ出す盤(とても長い 1 行)を作る
        page.evaluate("""() => {
          const wide = document.createElement('div');
          wide.id = 'wide'; wide.style.width = '2600px'; wide.style.height = '10px';
          document.getElementById('main').appendChild(wide);
        }""")
        page.wait_for_selector("#scroll-more-right", state="visible")
        self.assertIn("▶ 右にまだあります", page.text_content("#scroll-more-right"))
        page.evaluate("window.scrollTo(document.scrollingElement.scrollWidth, 0)")
        page.wait_for_selector("#scroll-more-right", state="hidden")
        page.evaluate("document.getElementById('wide').remove(); window.scrollTo(0, 0)")


class FitsTest(_Layout):
    """収まるラインでは、どの画面でも印は出ない。"""

    MODE = config.MODE_SITE
    LINE = "LVC"

    def test_収まる盤では印が出ない(self) -> None:
        for width, height in SCREENS:
            with self.subTest(screen=f"{width}x{height}"):
                page = self.at(width, height)
                got = self.layout(page)
                self.assertLessEqual(got["overY"], 4, got)
                self.assertFalse(got["hint"], got)
                self.assertFalse(got["right"], got)
                page.close()


if __name__ == "__main__":
    unittest.main()
