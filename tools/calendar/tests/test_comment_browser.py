"""コメント・連絡を**本物のブラウザ**で ──「xx班からの連絡」と残した時刻・宛先のラインの鍵 (VER4.2.4)

- 作業長以外の端末では、ラインの欄は**この端末のラインが選ばれたまま押せない**
  (選べるのは書いた班だけ)。見出しは「連絡を残す班」→「L-1 B班からの連絡」
- 登録したあと、マスにマウスを乗せたとき(title)と内容閲覧の両方に、
  「L-1 B班からの連絡」と「2026/10/21 14:05」の形の時刻が出る
- 作業長の端末だけラインを選べ、選んだラインの班に絞られる
- 画面の鍵を外して別のラインを送っても、**サーバが断り**、書かれない(画面だけの守りにしない)
"""
from __future__ import annotations

import datetime as _dt

from calendar_app import config
from calendar_app import settings as user_settings

from tests import _web
from tests._page_browser import PageBrowserCase

#: 画面に出す時刻の形(秒は出さない)
SHOWN_TIME = r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}(?!:)"


def _add_member(conn, code, name, group, line):
    conn.execute(
        f'INSERT INTO "{config.TABLE_MEMBER}" '
        '("管理番号","苗字","班","名前","読み","担当ライン") VALUES (?,?,?,?,?,?)',
        (code, name, group, name, name, line))
    conn.commit()


class CommentBrowserTests(PageBrowserCase):
    def setUp(self) -> None:
        super().setUp()
        _web.with_source(self)              # 書き先が無いと登録は断られる
        _add_member(self.conn, "10", "山田太郎", "B", "L-1")
        _add_member(self.conn, "11", "加藤眞一", "A", "L-1")
        _add_member(self.conn, "20", "鈴木一郎", "A", "HVC")
        _add_member(self.conn, "21", "佐藤次郎", "C", "HVC")
        self.addCleanup(user_settings.save_my_line, "")
        user_settings.save_my_line("L-1")
        today = _dt.date.today()
        self.date = today.strftime(config.DATE_KEY_FORMAT)

    # -- 手順 ------------------------------------------------------------
    def open_comment(self, page) -> None:
        page.wait_for_selector(f'.day[data-date="{self.date}"]')
        page.click(f'.day[data-date="{self.date}"]')
        page.click(".modal button.choice:has-text('コメント・連絡')")
        page.wait_for_selector("#cm-line")

    def options(self, page, sel: str) -> list[str]:
        return page.eval_on_selector(sel, "(s) => [...s.options].map((o) => o.value)")

    def title(self, page) -> str:
        return page.inner_text(".modal .modal__head h2")

    def write(self, page, text: str) -> None:
        page.click(".modal button:has-text('次へ')")
        page.wait_for_selector("#cm-text")
        page.fill("#cm-text", text)
        page.click(".modal button:has-text('登録する')")

    def comment_rows(self):
        return self.conn.execute(
            f'SELECT * FROM "{config.TABLE_DATA}" WHERE "区分"=?',
            (config.KUBUN_OTHER,)).fetchall()

    # -- 試験 ------------------------------------------------------------
    def test_作業長以外はラインを選べず_班からの連絡と時刻がマスと内容閲覧に出る(self) -> None:
        page = self.open("/calendar")
        self.open_comment(page)
        self.assertIn("連絡を残す班", self.title(page))
        self.assertNotIn("宛先", self.title(page))
        # ラインはこの端末のラインが選ばれたまま、押せない
        self.assertEqual(page.input_value("#cm-line"), "L-1")
        self.assertTrue(page.is_disabled("#cm-line"))
        self.assertEqual(self.options(page, "#cm-line"), ["L-1"])
        self.assertIn("作業長", page.inner_text(".modal"))
        # 班(書いた班)は選べる
        self.assertFalse(page.is_disabled("#cm-group"))
        self.assertEqual(self.options(page, "#cm-group"), ["A", "B"])
        page.select_option("#cm-group", "B")

        page.click(".modal button:has-text('次へ')")
        page.wait_for_selector("#cm-text")
        self.assertIn("L-1 B班からの連絡", self.title(page))
        self.assertNotIn("への連絡", self.title(page))
        page.fill("#cm-text", "2号機の段取り替えをします")
        page.click(".modal button:has-text('登録する')")
        page.wait_for_selector(".modal", state="detached")

        cell = f'.day[data-date="{self.date}"]'
        page.wait_for_function(
            "(sel) => (document.querySelector(sel).innerText || '').includes('L-1:B班 コメント有')",
            arg=cell)
        # マウスを乗せたとき(title)に、どの班からといつ
        tip = page.get_attribute(cell, "title") or ""
        self.assertIn("L-1 B班からの連絡", tip)
        self.assertRegex(tip, SHOWN_TIME)

        # 内容閲覧にも
        page.click(cell)
        page.click(".modal button.choice:has-text('内容閲覧')")
        page.wait_for_selector(".modal .viewer")
        viewer = page.inner_text(".modal .viewer")
        self.assertIn("L-1 B班からの連絡", viewer)
        self.assertRegex(viewer, SHOWN_TIME)
        self.assertIn("2号機の段取り替えをします", viewer)

        sent = self.posts_to("/api/comment")
        self.assertEqual([(b["line"], b["group"]) for b in sent], [("L-1", "B")])
        row = self.comment_rows()[0]
        self.assertEqual(row[config.COL_WRITER_GROUP], "B")
        # 画面に出た時刻は保存した時刻(秒を落としたもの)
        self.assertIn(row[config.COL_REGISTERED_AT][:16], viewer)
        self.assertEqual(self.errors, [])

    def test_作業長はラインを選べて_そのラインの班に絞られる(self) -> None:
        user_settings.save_my_line(config.LINE_SUPERVISOR)
        page = self.open("/calendar")
        self.open_comment(page)
        self.assertFalse(page.is_disabled("#cm-line"))
        self.assertEqual(sorted(self.options(page, "#cm-line")), ["HVC", "L-1"])
        page.select_option("#cm-line", "HVC")
        self.assertEqual(self.options(page, "#cm-group"), ["A", "C"])
        page.select_option("#cm-group", "C")
        page.click(".modal button:has-text('次へ')")
        page.wait_for_selector("#cm-text")
        self.assertIn("HVC C班からの連絡", self.title(page))
        page.fill("#cm-text", "作業長から")
        page.click(".modal button:has-text('登録する')")
        page.wait_for_selector(".modal", state="detached")
        self.wait_until(page, lambda: len(self.comment_rows()) == 1, "連絡が1行")
        row = self.comment_rows()[0]
        self.assertEqual((row["ライン"], row[config.COL_WRITER_GROUP]), ("HVC", "C"))
        self.assertEqual(self.errors, [])

    def test_画面の鍵を外して別のラインを送っても_サーバが断って書かない(self) -> None:
        page = self.open("/calendar")
        self.open_comment(page)
        self.assertTrue(page.is_disabled("#cm-line"))
        # 画面の守りを外して、よそのラインを選ばせる
        page.evaluate("""() => {
            const line = document.getElementById('cm-line');
            line.disabled = false;
            const opt = document.createElement('option');
            opt.value = 'HVC'; opt.textContent = 'HVC';
            line.appendChild(opt);
            line.value = 'HVC';
            const group = document.getElementById('cm-group');
            group.replaceChildren(Object.assign(document.createElement('option'), { value: 'A', textContent: 'A' }));
            group.value = 'A';
        }""")
        self.write(page, "よそのラインへ")
        # 断られた理由を出して、打った本文を持ったまま開き直す
        page.wait_for_selector("#cm-note")
        self.assertIn("作業長", page.inner_text("#cm-note"))
        self.assertEqual(page.input_value("#cm-text"), "よそのラインへ")
        self.assertEqual([b["line"] for b in self.posts_to("/api/comment")], ["HVC"])
        self.assertEqual(self.comment_rows(), [])
        self.assertEqual(self.errors, [])
