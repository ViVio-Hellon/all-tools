"""一部のフォルダが読めなかっただけで、その中の選択を永久に消さない(画面側)を**本物のブラウザ**で (段階1)

前は ``inspection.js`` の字(``unreadableFor(inv)``・``"isp.held"``・「読めたら戻します」)を探していた。
ここでは見本の点検表を2つ選んでおき、「再読込」で**1つのサブフォルダが読めなかった一覧**を返す。

- 読めなかったフォルダの中の選択は、いったん外して「読めたら戻します」と言う。ほかの選択は残る
- 次の再読込で読めたら、**元の順番の位置に**戻す(あいだに画面を開き直しても)
- 読めたのに無くなっていたら、そのとき解除する
"""
from __future__ import annotations

import copy
import json
import os
import unittest

from tests._page_browser import PageBrowserCase


class SelectionHeldBrowserTests(PageBrowserCase):
    def selected(self, page) -> list[str]:
        return page.evaluate("""() => [...document.querySelectorAll('#sel-list .sel-item__name')]
            .map((n) => n.textContent)""")

    def inventory_without(self, item: dict, *, unreadable: bool) -> dict:
        """`item` が見えない一覧(サーバの形のまま)。`unreadable` ならそのサブフォルダが読めなかった。"""
        data = copy.deepcopy(self.get("/api/inventory").get_json())
        inv = data["inventory"]
        for cat in inv["categories"]:
            for sub in cat["subcategories"]:
                sub["items"] = [it for it in sub["items"] if it["id"] != item["id"]]
        if unreadable:
            folder = os.path.join(inv["root"], item["category"], item["subcategory"])
            inv["errors"] = [{"path": folder, "message": "アクセスが拒否されました(試験)"}]
        return data

    def refresh(self, page, toast: str) -> None:
        page.click("#btn-refresh")
        page.wait_for_selector(f".toast:has-text('{toast}')")

    def test_読めなかったフォルダの選択はいったん外し_読めたら元の位置に戻す(self) -> None:
        first, second, third = self.item("消火器点検"), self.item("作業手順の確認"), self.item("クレーン点検")
        ctx = self.new_context()
        ids = [first["id"], second["id"], third["id"]]
        # 選んでおく(開き直したときは、画面が覚えている選択のまま)
        ctx.add_init_script("if (!sessionStorage.getItem('isp.selection')) "
                            f"sessionStorage.setItem('isp.selection', {json.dumps(json.dumps(ids))});")
        page = self.open("/", ctx)
        page.wait_for_selector("#sel-list li")
        self.assertEqual(self.selected(page), [first["name"], second["name"], third["name"]])

        # 2つ目のサブフォルダだけ、いま読めない
        self.answers["/api/inventory?"] = (200, self.inventory_without(second, unreadable=True))
        self.refresh(page, "読めたら戻します")
        self.assertEqual(self.selected(page), [first["name"], third["name"]])

        # 読めないあいだに画面を開き直しても、いったん外したことは覚えている
        page.reload(wait_until="load")
        page.wait_for_selector("#sel-list li")
        self.assertEqual(self.selected(page), [first["name"], third["name"]])

        # 読めた: 元の2番目に戻る
        del self.answers["/api/inventory?"]
        self.refresh(page, "1 件の選択を戻しました")
        self.assertEqual(self.selected(page), [first["name"], second["name"], third["name"]])

        # 読めたのに無い(消された・名前が変わった): そのとき解除する
        self.answers["/api/inventory?"] = (200, self.inventory_without(second, unreadable=False))
        self.refresh(page, "一覧から無くなったため")
        self.assertEqual(self.selected(page), [first["name"], third["name"]])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
