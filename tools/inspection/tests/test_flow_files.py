"""点検表のファイル・フォルダ・プレビュー・印刷の通しの流れ(現場の使い方どおりに続けて動かす)

現場の使い方:
- 点検表ルートフォルダ(共有フォルダのことが多い)> カテゴリー > サブカテゴリー > Excel・Word を一覧にし、
  選んでまとめて印刷する。プレビューは画像(Word は PDF)の控えを作って出す
  (控えのキーは パス + 更新日時 + 大きさ + 設定。「作り直す」は控えを使わずに作る)
- 起動したら前回の一覧の控えを先に出し、裏で調べる。**自動では調べ直さない**
  (「再読込」か次の起動。現場が了承した仕様)。一覧のあとで消えた点検表を選ぶと断る
- 印刷・プレビューは Excel / Word を COM で動かす(Windows)。ここでは模擬(Dummy)と
  偽の処理プログラム(fake_worker)で、Python の側の流れを確かめる

1台ぶんの PC(`_Pc`)は、**自分のローカル領域**(控え・利用者設定)・自分の業務・自分の画面
(Flask の test_client)を持つ。2台で同じ共有フォルダを使う流れもこれで作る。

**時間に頼らない**(Windows の CI は遅い): 印刷は `wait_idle`、検索は `wait`、印刷の途中は
模擬の中の合図(`threading.Event`)で待ってから次へ進む。Windows では開いているファイルを
消せない・名前を変えられないので、ファイルはどれも閉じてから触る。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from tests.helpers import make_tree, temp_dir

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

from app.services.excel_service import DummyExcelBackend
from core import event_log

TOKEN = "flow-token"
AUTH = {"X-Tool-Token": TOKEN}
WAIT = 20          # 状態を待つ上限(秒)。普段はすぐ返る。遅い CI でも落ちないよう長めに


class FlowBackend(DummyExcelBackend):
    """模擬の Excel・Word。作った回数・刷ったファイルを数え、印刷の途中で止めておける。

    ``hold_at`` 件目に入ったところで ``holding`` を立て、``release`` まで待つ
    (印刷中に中止・再読込・2回目を押す流れを、時間に頼らずに作る)。
    """

    def __init__(self) -> None:
        super().__init__(print_delay=0, preview_delay=0)
        self.previews: List[str] = []
        self.printed: List[str] = []
        self.hold_at = 0
        self.holding = threading.Event()
        self.release = threading.Event()

    def preview(self, path: str, pdf: bool = True):
        self.previews.append(path)
        return super().preview(path, pdf=pdf)

    def print_files(self, files, copies, on_event, should_continue):
        def watch(kind, index, ok=False):
            if kind == "end":
                on_event(kind, index, ok)
                if ok:
                    self.printed.append(files[index - 1])
                return
            on_event(kind, index)
            if index == self.hold_at:
                self.holding.set()
                self.release.wait(WAIT)

        return super().print_files(files, copies, watch, should_continue)


@unittest.skipUnless(HAS_WEB, "Flask が入っていないためスキップ")
class _FlowCase(unittest.TestCase):
    """共有フォルダ(``self.root``)と、それを使う PC を作る。"""

    FILES: List[str] = []

    def setUp(self) -> None:
        from core import idle_exit
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self._tmp = temp_dir()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.root = os.path.join(self.tmp, "共有", "一括管理_点検表")
        make_tree(self.root, self.FILES)

    def path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def pc(self, name: str = "PC1", root: Optional[str] = None, start: bool = True,
           restart: bool = False) -> "_Pc":
        """``restart=True`` は同じ PC で起動し直す(フォルダ設定はその PC に残っているものを使う)。"""
        pc = _Pc(self, name, "" if restart else (root or self.root))
        if start:
            pc.start()
        return pc

    @staticmethod
    def write(path: str, data: bytes, bump: float = 0.0) -> None:
        """中身を書き換える(更新日時も確かに変える。秒単位のファイルシステムでも同じにならない)。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(data)
        if bump:
            st = os.stat(path)
            os.utime(path, (st.st_atime, st.st_mtime + bump))


class _Pc:
    """1台の PC。自分のローカル領域(控え・利用者設定)・業務・画面を持つ。"""

    def __init__(self, case: _FlowCase, name: str, root: str) -> None:
        from app import create_app
        from app.business import InspectionBusiness
        self.case = case
        self.name = name
        self.local = os.path.join(case.tmp, f"local_{name}")
        # ローカル領域の場所は業務を作るときに決まる(控え・利用者設定・作業フォルダ)
        with mock.patch.dict(os.environ, {"INSPECTION_LOCAL_DIR": self.local}):
            self.biz = InspectionBusiness({})
            if root:
                self.biz.settings.update(root_folder=root)
        self.backend = FlowBackend()
        self.biz.excel.backend = self.backend          # Windows の CI でも本物の Excel を起こさない
        self.app = create_app(token=TOKEN, port=8799, options={}, business=self.biz)
        self.app.config["READY"] = True
        self.client = self.app.test_client()
        case.addCleanup(self._finish)

    def _finish(self) -> None:
        self.backend.release.set()
        self.biz.printing.wait_idle(WAIT)
        self.biz.inspection.wait(WAIT)

    # ---- 起動・再読込 ------------------------------------------------
    def start(self) -> None:
        """起動と同じ順: 前回の一覧の控えを出し、裏で調べる(終わるまで待つ)。"""
        self.biz.preview_cache.prune()
        self.biz.inspection.load_cache()
        self.biz.inspection.start_scan("起動時")
        self.case.assertTrue(self.biz.inspection.wait(WAIT), "起動時の検索が終わらない")

    def refresh(self) -> Dict[str, Any]:
        """画面の「再読込」。調べ終わるまで待って、一覧を返す。"""
        res = self.post("/api/inventory/refresh")
        self.case.assertEqual(res.status_code, 200, res.get_json())
        self.case.assertTrue(self.biz.inspection.wait(WAIT), "再読込の検索が終わらない")
        return self.inventory()

    # ---- 画面の要求 --------------------------------------------------
    def get(self, path: str):
        return self.client.get(path, headers=AUTH)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None):
        return self.client.post(path, json=body or {}, headers=AUTH)

    def inventory(self) -> Dict[str, Any]:
        res = self.get("/api/inventory")
        self.case.assertEqual(res.status_code, 200)
        return res.get_json()["inventory"]

    def items(self) -> Dict[str, Dict[str, Any]]:
        """一覧の点検表(ファイル名 → 点検表)。"""
        inv = self.inventory()
        return {it["file_name"]: it for c in inv["categories"] for s in c["subcategories"] for it in s["items"]}

    def preview(self, item_id: str, force: bool = False, image: bool = False):
        return self.post("/api/preview", {"id": item_id, "force": force, "image": image})

    def print(self, ids: List[str], copies: Any = 1):
        return self.post("/api/print", {"ids": ids, "copies": copies})

    def wait_print(self) -> Dict[str, Any]:
        self.case.assertTrue(self.biz.printing.wait_idle(WAIT), "印刷が終わらない")
        return self.get("/api/print/status").get_json()["job"]

    def cache_files(self) -> List[str]:
        folder = self.biz.preview_cache.dir
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


def _names(inv: Dict[str, Any]) -> List[str]:
    return [it["name"] for c in inv["categories"] for s in c["subcategories"] for it in s["items"]]


def _print_items(ref: str) -> List[Dict[str, Any]]:
    """その印刷の1件ずつの記録(なぜなぜ用)。書いた順。"""
    rows = [e for e in event_log.read(ref=ref) if e.get("op") == "print.item"]
    return sorted(rows, key=lambda e: e.get("index", 0))


# ======================================================================
# 1. 一覧のあとで点検表を 足す・消す・名前を変える・直す → 再読込
# ======================================================================
class FileChangeFlowTests(_FlowCase):
    FILES = ["梱包/日常点検/梱包_日常点検_台車.xlsx",
             "梱包/日常点検/梱包_日常点検_コンベア.xlsx",
             "梱包/日常点検/梱包_日常点検_消火器.xlsx"]

    def setUp(self) -> None:
        super().setUp()
        for rel in self.FILES:
            self.write(self.path(rel), rel.encode("utf-8"))
        self.me = self.pc()

    def test_足した点検表は再読込で出る_それまでは自動で調べ直さない(self) -> None:
        self.assertEqual(_names(self.me.inventory()), ["コンベア", "台車", "消火器"])
        self.write(self.path("梱包/日常点検/梱包_日常点検_非常灯.xlsx"), b"new")
        self.write(self.path("梱包/月次点検/梱包_月次点検_避難器具.xlsx"), b"new")   # 新しいサブカテゴリー
        # 自動では調べ直さない(現場が了承した仕様)
        self.assertNotIn("非常灯", _names(self.me.inventory()))
        inv = self.me.refresh()
        self.assertIn("非常灯", _names(inv))
        self.assertIn("避難器具", _names(inv))
        new = self.me.items()["梱包_日常点検_非常灯.xlsx"]
        res = self.me.preview(new["id"])
        self.assertEqual(res.status_code, 200, res.get_json())
        self.me.print([new["id"]])
        self.assertEqual(self.me.wait_print()["state"], "done")

    def test_消した点検表_再読込の前は見つからないと断り_ほかは刷れる(self) -> None:
        items = self.me.items()
        ids = [items[f"梱包_日常点検_{n}.xlsx"]["id"] for n in ("台車", "コンベア", "消火器")]
        os.remove(self.path("梱包/日常点検/梱包_日常点検_コンベア.xlsx"))
        # プレビュー: 控えの絵を出さずに「見つかりません」
        res = self.me.preview(ids[1])
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "FILE_NOT_FOUND")
        # 印刷: 消えた1件だけ失敗し、ほかは刷られて理由が残る
        self.assertEqual(self.me.print(ids).status_code, 202)
        job = self.me.wait_print()
        self.assertEqual(job["state"], "done")
        self.assertEqual([i["status"] for i in job["items"]], ["ok", "error", "ok"])
        self.assertEqual(job["items"][1]["code"], "FILE_NOT_FOUND")
        self.assertEqual(job["items"][1]["message"], "対象の点検表ファイルが見つかりません。")
        self.assertEqual(job["message"], "一部の点検表を印刷できませんでした")
        self.assertEqual(len(self.me.backend.printed), 2)
        rows = _print_items(job["ref"])
        self.assertEqual([r["result"] for r in rows], [event_log.OK, event_log.NG, event_log.OK])
        self.assertEqual(rows[1]["code"], "FILE_NOT_FOUND")
        self.assertTrue(rows[1]["file"].endswith("梱包_日常点検_コンベア.xlsx"))

    def test_消した点検表_再読込のあとは一覧から消え_前の番号は再読込を促して断る(self) -> None:
        old = self.me.items()["梱包_日常点検_コンベア.xlsx"]["id"]
        keep = self.me.items()["梱包_日常点検_台車.xlsx"]["id"]
        os.remove(self.path("梱包/日常点検/梱包_日常点検_コンベア.xlsx"))
        inv = self.me.refresh()
        self.assertEqual(_names(inv), ["台車", "消火器"])
        for res in (self.me.preview(old), self.me.print([keep, old])):
            self.assertEqual(res.status_code, 409)
            body = res.get_json()["error"]
            self.assertEqual(body["code"], "ITEM_NOT_FOUND")
            self.assertIn("再読込", body["message"])
        self.assertEqual(self.me.backend.printed, [], "1件でも古ければ刷り始めない(選び直してから)")

    def test_名前を変えた点検表は別の点検表になり_前の名前では刷らない(self) -> None:
        before = self.me.items()["梱包_日常点検_台車.xlsx"]
        self.assertEqual(self.me.preview(before["id"]).status_code, 200)
        os.rename(self.path("梱包/日常点検/梱包_日常点検_台車.xlsx"),
                  self.path("梱包/日常点検/梱包_日常点検_台車(新).xlsx"))
        # 再読込の前: 前の名前は見つからない
        self.assertEqual(self.me.preview(before["id"]).get_json()["error"]["code"], "FILE_NOT_FOUND")
        items = self.me.refresh() and self.me.items()
        self.assertNotIn("梱包_日常点検_台車.xlsx", items)
        after = items["梱包_日常点検_台車(新).xlsx"]
        self.assertEqual(after["name"], "台車(新)")
        self.assertNotEqual(after["id"], before["id"])
        res = self.me.preview(after["id"])
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["cached"], "名前が変われば前の控えは使わない(場所が違う)")
        self.assertEqual(self.me.print([before["id"]]).status_code, 409)

    def test_中身を直した点検表は同じ番号のまま別の控えになる(self) -> None:
        item = self.me.items()["梱包_日常点検_消火器.xlsx"]
        first = self.me.preview(item["id"]).get_json()
        again = self.me.preview(item["id"]).get_json()
        self.assertEqual((first["cached"], again["cached"]), (False, True))
        self.assertEqual(first["key"], again["key"])
        self.write(self.path("梱包/日常点検/梱包_日常点検_消火器.xlsx"), b"fixed contents", bump=5)
        # 再読込しなくても、直したファイルの控えは作り直す(キーは開くときの更新日時と大きさ)
        fixed = self.me.preview(item["id"]).get_json()
        self.assertFalse(fixed["cached"])
        self.assertNotEqual(fixed["key"], first["key"])
        self.assertEqual(len(self.me.backend.previews), 2)
        # 再読込すると番号は同じで、更新日時・大きさが新しくなる
        after = self.me.refresh() and self.me.items()["梱包_日常点検_消火器.xlsx"]
        self.assertEqual(after["id"], item["id"])
        self.assertGreater(after["modified"], item["modified"])
        self.assertEqual(after["size"], len(b"fixed contents"))
        self.assertTrue(self.me.preview(item["id"]).get_json()["cached"])

    def test_次の起動は前回の控えを先に出し_調べ終わったら新しい一覧になる(self) -> None:
        os.remove(self.path("梱包/日常点検/梱包_日常点検_台車.xlsx"))
        self.write(self.path("梱包/日常点検/梱包_日常点検_非常灯.xlsx"), b"x")
        nxt = self.pc("PC1", start=False, restart=True)              # 同じ PC(同じローカル領域)で起動し直す
        self.assertTrue(nxt.biz.inspection.load_cache())
        self.assertEqual(_names(nxt.inventory()), ["コンベア", "台車", "消火器"], "前回の控えを先に出す")
        self.assertTrue(nxt.get("/api/inventory").get_json()["scan"]["from_cache"])
        nxt.biz.inspection.start_scan("起動時")
        self.assertTrue(nxt.biz.inspection.wait(WAIT))
        self.assertEqual(_names(nxt.inventory()), ["コンベア", "消火器", "非常灯"])
        self.assertFalse(nxt.get("/api/inventory").get_json()["scan"]["from_cache"])


# ======================================================================
# 2. 開いたまま・読み取り専用・0バイト・長い名前・日本語や記号・深いフォルダ・一時ファイル
# ======================================================================
class OddFilesFlowTests(_FlowCase):
    SYMBOLS = "c_s_点検 表 (第2版) #1 &100%+=;,'[]{}!~＠①.xlsx"
    FILES = ["c/s/c_s_普通.xlsx",
             f"c/s/{SYMBOLS}",
             "c/s/c_s_手順 書 #2 &.docx",
             "c/s/~$c_s_普通.xlsx",              # Excel が開いているあいだの所有者ファイル
             "c/s/~$s_手順 書 #2 &.docx",         # Word の所有者ファイル(名前の頭を置き換える)
             "c/s/._c_s_普通.xlsx",               # Mac が共有フォルダに置く影のファイル
             "c/s/c_s_空.xlsx",                   # 0バイト
             "c/s/もっと深い/c_s_深い.xlsx",       # サブカテゴリーの下のフォルダ(VBA 版と同じく見ない)
             "c/c_カテゴリ直下.xlsx",
             "ルート直下.xlsx"]

    def setUp(self) -> None:
        super().setUp()
        self.write(self.path("c/s/c_s_普通.xlsx"), b"normal")
        self.write(self.path(f"c/s/{self.SYMBOLS}"), b"symbols")

    def test_一時ファイルや深いフォルダのものは載せず_影のファイルは命名規則外に回す(self) -> None:
        me = self.pc()
        inv = me.inventory()
        sub = inv["categories"][0]["subcategories"][0]
        self.assertEqual(sorted(it["file_name"] for it in sub["items"]),
                         sorted(["c_s_普通.xlsx", self.SYMBOLS, "c_s_手順 書 #2 &.docx", "c_s_空.xlsx"]))
        self.assertEqual([u["file_name"] for u in sub["unmatched"]], ["._c_s_普通.xlsx"])
        self.assertEqual(inv["listed_files"], 4)
        self.assertEqual([c["name"] for c in inv["categories"]], ["c"])
        self.assertEqual([s["name"] for s in inv["categories"][0]["subcategories"]], ["s"])

    def test_日本語や空白や記号の名前でもプレビューと印刷ができる(self) -> None:
        me = self.pc()
        items = me.items()
        sym = items[self.SYMBOLS]
        self.assertEqual(sym["name"], "点検 表 (第2版) #1 &100%+=;,'[]{}!~＠①")
        res = me.preview(sym["id"])
        self.assertEqual(res.status_code, 200, res.get_json())
        word = items["c_s_手順 書 #2 &.docx"]
        data = me.preview(word["id"]).get_json()
        pdf = me.get(data["pdf_url"])
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        # 名前は PDF の題に出る。日本語・空白・記号は filename* に符号化して入れる(壊さない)
        disposition = pdf.headers["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline;"), disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertTrue(disposition.isascii(), disposition)
        me.print([sym["id"], word["id"], items["c_s_空.xlsx"]["id"]])
        job = me.wait_print()
        self.assertEqual((job["state"], job["failed"]), ("done", 0))
        self.assertIn(self.path(f"c/s/{self.SYMBOLS}"), me.backend.printed)

    def test_開いたまま_読み取り専用のファイルも一覧に出て刷れる(self) -> None:
        import stat
        target = self.path("c/s/c_s_普通.xlsx")
        os.chmod(target, stat.S_IREAD)
        self.addCleanup(os.chmod, target, stat.S_IREAD | stat.S_IWRITE)
        with open(target, "rb"):                        # 開いたまま(読むだけの人がいる)
            me = self.pc()
            item = me.items()["c_s_普通.xlsx"]
            self.assertEqual(me.preview(item["id"]).status_code, 200)
            me.print([item["id"]])
            self.assertEqual(me.wait_print()["failed"], 0)

    def test_ほかのPCが開いている点検表は_所有者ファイルを載せずに刷れて知らせが出る(self) -> None:
        """本物と同じ手順の偽の処理プログラムで刷る(Excel は読み取り専用で開く)。"""
        from tests.test_excel_protocol import FakeBackend
        owner = "山田"
        data = bytes([len(owner)]) + owner.encode("cp932").ljust(53, b" ")
        data = data[:53] + bytes([len(owner), 0]) + owner.encode("utf-16-le")
        self.write(self.path("c/s/~$c_s_普通.xlsx"), data)
        me = self.pc()
        backend = FakeBackend(me.biz.cfg, os.path.join(me.local, "work"), me.biz.registry, me.biz.log)
        FakeBackend.extra = {}
        self.addCleanup(backend.shutdown, "試験の後片付け")
        me.biz.excel.backend = backend
        items = me.items()
        self.assertNotIn("~$c_s_普通.xlsx", items)
        me.print([items["c_s_普通.xlsx"]["id"], items[self.SYMBOLS]["id"]])
        job = me.wait_print()
        self.assertEqual([i["status"] for i in job["items"]], ["ok", "ok"])
        self.assertEqual(job["items"][0]["notice"], "山田さんが「c_s_普通」を開いたままです(印刷はできます)")
        self.assertEqual(job["items"][1]["notice"], "")

    def test_長いパスと深い共有フォルダ(self) -> None:
        deep = os.path.join(self.tmp, "共有", *(["とても長い名前のフォルダ" * 3] * 4))
        long_name = "長" * 30                              # 1つの名前は 255 バイトまで(Linux)
        rel = f"カテゴリー{long_name}/サブ{long_name}/カテゴリー{long_name}_サブ{long_name}_点検.xlsx"
        try:
            make_tree(deep, [rel])
        except OSError as exc:                           # 長いパスを許していない Windows
            self.skipTest(f"この PC では長いパスを作れない: {exc}")
        target = os.path.join(deep, *rel.split("/"))
        self.assertGreater(len(target), 300)
        me = self.pc(root=deep)
        items = me.items()
        self.assertEqual(list(items), [os.path.basename(target)])
        item = items[os.path.basename(target)]
        self.assertEqual(item["name"], "点検")
        self.assertEqual(me.preview(item["id"]).status_code, 200)
        me.print([item["id"]])
        self.assertEqual(me.wait_print()["failed"], 0)
        self.assertEqual(me.backend.printed, [target])


# ======================================================================
# 3. 点検表ルートフォルダが見えない・途中で見えなくなる・別のフォルダに変える
# ======================================================================
class FolderFlowTests(_FlowCase):
    FILES = ["梱包/日常/梱包_日常_台車.xlsx", "梱包/日常/梱包_日常_コンベア.xlsx", "検査/月次/検査_月次_外観.xlsx"]

    def setUp(self) -> None:
        super().setUp()
        self.other = os.path.join(self.tmp, "別の共有", "点検表")
        make_tree(self.other, ["出荷/週次/出荷_週次_フォーク.xlsx"])
        self.away = self.root + "_落ちている"
        self.me = self.pc()
        self.ids = {k: v["id"] for k, v in self.me.items().items()}
        self.cache_path = self.me.biz.inspection.cache_path

    def share_down(self) -> None:
        os.rename(self.root, self.away)

    def share_up(self) -> None:
        os.rename(self.away, self.root)

    def cached_root_and_names(self):
        with open(self.cache_path, encoding="utf-8") as fp:
            data = json.load(fp)
        return data["root"], sorted(i["name"] for c in data["categories"]
                                    for s in c["subcategories"] for i in s["items"])

    def test_共有が落ちたら一覧は空で控えは残し_戻れば同じ番号で出る(self) -> None:
        self.share_down()
        inv = self.me.refresh()
        self.assertFalse(inv["root_exists"])
        self.assertEqual(_names(inv), [])
        # 前回の一覧の控えは、見えない一覧で上書きしない(次の起動でも前回の一覧を先に出せる)
        self.assertEqual(self.cached_root_and_names(), (self.root, ["コンベア", "台車", "外観"]))
        res = self.me.preview(self.ids["梱包_日常_台車.xlsx"])
        self.assertEqual(res.status_code, 409)
        self.assertIn("再読込", res.get_json()["error"]["message"])
        scan = [e for e in event_log.read() if e.get("op") == "scan" and e.get("folder") == self.root]
        self.assertEqual(scan[0]["code"], "FOLDER_NOT_FOUND")
        # 共有が落ちたまま起動し直す: 前回の控えを先に出し、調べ終わったら空(見えない)になる
        nxt = self.pc("PC1", start=False, restart=True)
        self.assertTrue(nxt.biz.inspection.load_cache())
        self.assertEqual(len(nxt.items()), 3)
        nxt.biz.inspection.start_scan("起動時")
        self.assertTrue(nxt.biz.inspection.wait(WAIT))
        self.assertFalse(nxt.inventory()["root_exists"])
        # 戻った: 再読込で同じ番号のまま出る(画面で選んでおいたものがそのまま使える)
        self.share_up()
        self.assertEqual({k: v["id"] for k, v in nxt.refresh() and nxt.items().items()}, self.ids)
        nxt.print(list(self.ids.values()))
        self.assertEqual(nxt.wait_print()["succeeded"], 3)

    def test_一覧のあとで共有が落ちたらプレビュー印刷は見つからないと断る(self) -> None:
        self.share_down()
        res = self.me.preview(self.ids["梱包_日常_台車.xlsx"])
        self.assertEqual(res.get_json()["error"]["code"], "FILE_NOT_FOUND")
        self.me.print(list(self.ids.values()))
        job = self.me.wait_print()
        self.assertEqual(job["failed"], 3)
        self.assertEqual({i["code"] for i in job["items"]}, {"FILE_NOT_FOUND"})

    def test_調べている途中で共有が落ちたら_読めなかったことを残し控えは上書きしない(self) -> None:
        from app.services import inspection_service
        original = inspection_service._list_files
        dropped = []

        def drop_then_list(path, exts, errors):
            if not dropped:
                dropped.append(path)
                self.share_down()                       # 1つ目のサブカテゴリーを読む直前に落ちた
            return original(path, exts, errors)

        with mock.patch.object(inspection_service, "_list_files", drop_then_list):
            inv = self.me.refresh()
        self.assertTrue(dropped)
        # 初めから見えなかったときと同じ(画面は「見えません」を出し、選んだものを残す)
        self.assertFalse(inv["root_exists"], "途中まで読めた一覧を、見えている一覧として出した")
        self.assertEqual(_names(inv), [])
        self.assertEqual(self.cached_root_and_names(), (self.root, ["コンベア", "台車", "外観"]),
                         "途中で落ちた一覧で、前回の一覧の控えを上書きしない")
        scan = [e for e in event_log.read() if e.get("op") == "scan" and e.get("folder") == self.root]
        self.assertEqual(scan[0]["code"], "FOLDER_NOT_FOUND")
        self.share_up()
        self.assertEqual(len(self.me.refresh() and self.me.items()), 3)

    def test_点検表フォルダそのものを読めなかったら_空の一覧で控えを潰さない(self) -> None:
        """フォルダは在るが中を読めない(権限・共有の瞬断)。読めなかったことは画面と記録に出す。"""
        from app.services import inspection_service
        original = inspection_service._list_dirs

        def deny_root(path, errors):
            if path == self.root:
                errors.append({"path": path, "message": "フォルダを読み取れません: アクセスが拒否されました。"})
                return []
            return original(path, errors)

        with mock.patch.object(inspection_service, "_list_dirs", deny_root):
            inv = self.me.refresh()
        self.assertTrue(inv["root_exists"])
        self.assertEqual(inv["errors"][0]["path"], self.root)
        self.assertEqual(self.cached_root_and_names(), (self.root, ["コンベア", "台車", "外観"]))
        scan = [e for e in event_log.read() if e.get("op") == "scan" and e.get("folder") == self.root]
        self.assertEqual(scan[0]["code"], "READ_ERRORS")
        self.assertEqual(len(self.me.refresh() and self.me.items()), 3)

    def test_別のフォルダに変えたら一覧と控えが替わり_前の点検表は刷らない(self) -> None:
        old = self.ids["梱包_日常_台車.xlsx"]
        self.assertEqual(self.me.preview(old).status_code, 200)
        res = self.me.post("/api/settings", {"root_folder": self.other})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(res.get_json()["rescan"])
        self.assertTrue(self.me.biz.inspection.wait(WAIT))
        inv = self.me.inventory()
        self.assertEqual(inv["root"], self.other)
        self.assertEqual(_names(inv), ["フォーク"])
        for res in (self.me.preview(old), self.me.print([old])):
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.get_json()["error"]["code"], "ITEM_NOT_FOUND")
        new = self.me.items()["出荷_週次_フォーク.xlsx"]["id"]
        self.assertEqual(self.me.preview(new).status_code, 200)
        self.assertEqual(self.cached_root_and_names(), (self.other, ["フォーク"]))
        # 次の起動は新しいフォルダの控えを先に出す
        nxt = self.pc("PC1", start=False, restart=True)
        self.assertTrue(nxt.biz.inspection.load_cache())
        self.assertEqual(_names(nxt.inventory()), ["フォーク"])
        # 前のフォルダへ戻す: 前の番号が使え、プレビューの控えも(ファイルが同じなら)使える
        res = nxt.post("/api/settings", {"root_folder": self.root})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(nxt.biz.inspection.wait(WAIT))
        self.assertEqual(nxt.preview(old).status_code, 200)
        self.assertEqual(nxt.biz.settings.effective_root_folder(), self.root)

    def test_見えないフォルダには変えられず_いまの一覧のまま(self) -> None:
        res = self.me.post("/api/settings", {"root_folder": os.path.join(self.tmp, "無い")})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.get_json()["error"]["code"], "FOLDER_NOT_FOUND")
        self.assertEqual(self.me.biz.settings.effective_root_folder(), self.root)
        self.assertEqual(len(self.me.items()), 3)


# ======================================================================
# 4. 2台の PC が同じ共有フォルダを使う
# ======================================================================
class TwoPcFlowTests(_FlowCase):
    FILES = ["梱包/日常/梱包_日常_台車.xlsx", "梱包/日常/梱包_日常_コンベア.xlsx", "梱包/日常/梱包_日常_手順.docx"]

    def snapshot(self) -> List[str]:
        out = []
        for folder, _dirs, files in os.walk(self.root):
            out.extend(os.path.relpath(os.path.join(folder, f), self.root) for f in files)
        return sorted(out)

    def test_控えはそれぞれのPCに置き_共有フォルダには何も書かない(self) -> None:
        before = self.snapshot()
        pc1, pc2 = self.pc("ライン1"), self.pc("ライン2")
        self.assertNotEqual(pc1.biz.preview_cache.dir, pc2.biz.preview_cache.dir)
        self.assertNotEqual(pc1.biz.inspection.cache_path, pc2.biz.inspection.cache_path)
        ids1, ids2 = pc1.items(), pc2.items()
        self.assertEqual({k: v["id"] for k, v in ids1.items()}, {k: v["id"] for k, v in ids2.items()})
        for name in ids1:
            a = pc1.preview(ids1[name]["id"]).get_json()
            b = pc2.preview(ids2[name]["id"]).get_json()
            self.assertEqual(a["key"], b["key"])
            self.assertFalse(b["cached"], "ほかの PC の控えは見ない(自分の PC で作る)")
        self.assertEqual(pc1.cache_files(), pc2.cache_files())
        self.assertEqual(len(pc1.cache_files()), 6)       # 3件 × (絵・PDF + 情報)
        # 片方の控えを消しても、もう片方は控えのまま
        shutil.rmtree(pc1.biz.preview_cache.dir)
        self.assertTrue(pc2.preview(ids2["梱包_日常_台車.xlsx"]["id"]).get_json()["cached"])
        self.assertEqual(self.snapshot(), before, "共有フォルダに控え・一時ファイルを書いた")
        for pc in (pc1, pc2):
            self.assertFalse(os.path.commonpath([pc.local, self.root]) == self.root)

    def test_2台で同時に印刷してもぶつからない(self) -> None:
        pc1, pc2 = self.pc("ライン1"), self.pc("ライン2")
        ids = [v["id"] for v in pc1.items().values()]
        for pc in (pc1, pc2):
            pc.backend.hold_at = 1
        self.assertEqual(pc1.print(ids).status_code, 202)
        self.assertEqual(pc2.print(list(reversed(ids))).status_code, 202)
        # 2台とも1件目を刷っている途中(同時に印刷している)
        self.assertTrue(pc1.backend.holding.wait(WAIT))
        self.assertTrue(pc2.backend.holding.wait(WAIT))
        self.assertTrue(pc1.get("/api/print/status").get_json()["job"]["active"])
        self.assertTrue(pc2.get("/api/print/status").get_json()["job"]["active"])
        pc1.backend.release.set()
        pc2.backend.release.set()
        j1, j2 = pc1.wait_print(), pc2.wait_print()
        self.assertEqual((j1["succeeded"], j2["succeeded"]), (3, 3))
        self.assertNotEqual(j1["job_id"], j2["job_id"])
        self.assertEqual([i["name"] for i in j2["items"]], list(reversed([i["name"] for i in j1["items"]])))

    def test_ほかのPCで直した点検表は_再読込しなくても新しい控えを作る(self) -> None:
        pc1, pc2 = self.pc("ライン1"), self.pc("ライン2")
        item = pc1.items()["梱包_日常_台車.xlsx"]["id"]
        first = pc1.preview(item).get_json()
        self.write(self.path("梱包/日常/梱包_日常_台車.xlsx"), b"edited on line 2", bump=5)   # ライン2で直した
        again = pc1.preview(item).get_json()
        self.assertFalse(again["cached"])
        self.assertNotEqual(again["key"], first["key"])
        # ライン2で足した点検表は、ライン1では再読込するまで出ない(自動では調べ直さない)
        self.write(self.path("梱包/日常/梱包_日常_非常灯.xlsx"), b"x")
        pc2.refresh()
        self.assertIn("梱包_日常_非常灯.xlsx", pc2.items())
        self.assertNotIn("梱包_日常_非常灯.xlsx", pc1.items())
        self.assertIn("梱包_日常_非常灯.xlsx", pc1.refresh() and pc1.items())


# ======================================================================
# 6. まとめて印刷: 1件の失敗・取り消し・続けて2回・印刷中の再読込
# ======================================================================
class PrintFlowTests(_FlowCase):
    FILES = ["c/s/c_s_A.xlsx", "c/s/c_s_B.xlsx", "c/s/c_s_C.docx", "c/s/c_s_fail_D.xlsx"]

    def setUp(self) -> None:
        super().setUp()
        self.me = self.pc()
        self.items = self.me.items()
        self.ids = [self.items[f"c_s_{n}"]["id"] for n in ("A.xlsx", "B.xlsx", "C.docx")]

    def test_1件だけ開けなくてもほかは刷られ_失敗の理由が画面と記録に残る(self) -> None:
        """本物と同じ手順の偽の処理プログラムで(fail を含む名前は「プリンターがオフライン」で失敗する)。"""
        from tests.test_excel_protocol import FakeBackend
        backend = FakeBackend(self.me.biz.cfg, os.path.join(self.me.local, "work"), self.me.biz.registry,
                              self.me.biz.log)
        FakeBackend.extra = {}
        self.addCleanup(backend.shutdown, "試験の後片付け")
        self.me.biz.excel.backend = backend
        # (一覧のあとで消されたファイルは、模擬で test_消した点検表_… が見る。偽の処理プログラムは
        #  ファイルの有無を見ない。本物の excel_worker.vbs は FILE_NOT_FOUND で次へ進む)
        ids = [self.ids[0], self.items["c_s_fail_D.xlsx"]["id"], self.ids[1]]
        self.assertEqual(self.me.print(ids, "2").status_code, 202)
        job = self.me.wait_print()
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["copies"], 2)
        self.assertEqual([i["status"] for i in job["items"]], ["ok", "error", "ok"])
        self.assertEqual(job["items"][1]["code"], "PRINT_FAILED")
        self.assertEqual(job["items"][1]["message"], "印刷に失敗しました。")
        self.assertIn("オフライン", job["items"][1]["detail"])
        rows = _print_items(job["ref"])
        self.assertEqual([r["result"] for r in rows], [event_log.OK, event_log.NG, event_log.OK])
        self.assertIn("オフライン", rows[1]["detail"])
        end = [e for e in event_log.read(ref=job["ref"]) if e.get("op") == "print.end"]
        self.assertEqual((end[0]["succeeded"], end[0]["failed"]), (2, 1))

    def test_途中で取り消すと_いまの1件を刷り終えて残りは刷らない(self) -> None:
        self.me.backend.hold_at = 2
        self.me.print(self.ids)
        self.assertTrue(self.me.backend.holding.wait(WAIT))
        res = self.me.post("/api/print/cancel")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["job"]["cancel_requested"])
        self.me.backend.release.set()
        job = self.me.wait_print()
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual([i["status"] for i in job["items"]], ["ok", "ok", "skipped"])
        self.assertEqual(len(self.me.backend.printed), 2)
        self.assertEqual(self.me.post("/api/print/cancel").status_code, 409, "終わったあとの取り消しは断る")

    def test_続けて2回押しても2回目は断り_終わったらまた刷れる(self) -> None:
        self.me.backend.hold_at = 1
        self.assertEqual(self.me.print(self.ids).status_code, 202)
        self.assertTrue(self.me.backend.holding.wait(WAIT))
        res = self.me.print(self.ids)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error"]["code"], "PRINT_RUNNING")
        self.assertIn("1/3 件目", res.get_json()["error"]["message"])
        self.me.backend.release.set()
        first = self.me.wait_print()
        self.assertEqual(first["succeeded"], 3)
        self.me.backend.hold_at = 0
        self.assertEqual(self.me.print(self.ids).status_code, 202)
        second = self.me.wait_print()
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.assertEqual(len(self.me.backend.printed), 6)

    def test_印刷中に再読込しても_刷っている分は選んだときのファイルで続く(self) -> None:
        cached = self.me.preview(self.ids[0]).get_json()
        self.me.backend.hold_at = 1
        self.me.print(self.ids)
        self.assertTrue(self.me.backend.holding.wait(WAIT))
        os.remove(self.path("c/s/c_s_C.docx"))
        self.write(self.path("c/s/c_s_E.xlsx"), b"new")
        inv = self.me.refresh()                          # 印刷中でも一覧は読み直せる
        self.assertEqual(sorted(_names(inv)), ["A", "B", "E", "fail_D"])
        # 印刷中でも、控えのあるプレビューは出る(Excel を待たない)
        self.assertTrue(self.me.preview(self.ids[0]).get_json()["cached"])
        self.assertEqual(self.me.preview(self.ids[0]).get_json()["key"], cached["key"])
        self.me.backend.release.set()
        job = self.me.wait_print()
        self.assertEqual([i["status"] for i in job["items"]], ["ok", "ok", "error"])
        self.assertEqual(job["items"][2]["code"], "FILE_NOT_FOUND")

    def test_印刷中にフォルダを変えても_刷っている分は前のフォルダのまま続く(self) -> None:
        other = os.path.join(self.tmp, "別")
        make_tree(other, ["x/y/x_y_Z.xlsx"])
        self.me.backend.hold_at = 1
        self.me.print(self.ids)
        self.assertTrue(self.me.backend.holding.wait(WAIT))
        self.assertEqual(self.me.post("/api/settings", {"root_folder": other}).status_code, 200)
        self.assertTrue(self.me.biz.inspection.wait(WAIT))
        self.assertEqual(_names(self.me.inventory()), ["Z"])
        self.me.backend.release.set()
        job = self.me.wait_print()
        self.assertEqual(job["succeeded"], 3)
        self.assertTrue(all(p.startswith(self.root) for p in self.me.backend.printed))


# ======================================================================
# 5. プレビューの作り直し・控え
# ======================================================================
class PreviewCacheFlowTests(_FlowCase):
    FILES = ["検査/月次/検査_月次_外観.xlsx", "検査/月次/検査_月次_手順書.docx"]

    def setUp(self) -> None:
        super().setUp()
        self.me = self.pc()
        self.items = self.me.items()
        self.excel = self.items["検査_月次_外観.xlsx"]["id"]
        self.word = self.items["検査_月次_手順書.docx"]["id"]

    def image(self, data: Dict[str, Any]):
        url = data["image_url"] or data["pdf_url"]
        res = self.me.get(f"{url}?g={data['generated_at']}")
        return res

    def test_作り直すは控えを使わずに作り_作った時刻が変わる(self) -> None:
        for item_id in (self.excel, self.word):
            first = self.me.preview(item_id).get_json()
            cached = self.me.preview(item_id).get_json()
            self.assertTrue(cached["cached"])
            self.assertEqual(cached["generated_at"], first["generated_at"])
            made = len(self.me.backend.previews)
            # 時計の刻みが粗い PC でも「作った時刻」が同じにならないよう、刻みを越えてから押す
            deadline = time.time() + WAIT
            while time.time() <= first["generated_at"] and time.time() < deadline:
                time.sleep(0.01)
            again = self.me.preview(item_id, force=True).get_json()
            self.assertFalse(again["cached"])
            self.assertEqual(len(self.me.backend.previews), made + 1, "作り直すは Excel・Word で作る")
            self.assertEqual(again["key"], first["key"], "同じファイルなら同じキー(道は作った時刻で変える)")
            self.assertNotEqual(again["generated_at"], first["generated_at"],
                                "作った時刻が変わらないと、画面はブラウザの控えの前の絵を出す")
            self.assertEqual(self.image(again).status_code, 200)
            # 作り直したあとは、作り直したものが控えになる
            self.assertEqual(self.me.preview(item_id).get_json()["generated_at"], again["generated_at"])
        self.assertEqual(self.me.preview(self.word).get_json()["format"], "pdf")
        self.assertEqual(self.me.preview(self.excel).get_json()["format"], "image")

    def test_控えの情報が壊れていたら作り直す(self) -> None:
        data = self.me.preview(self.excel).get_json()
        with open(os.path.join(self.me.biz.preview_cache.dir, data["key"] + ".json"), "w",
                  encoding="utf-8") as fp:
            fp.write('{"file": "')                       # 書きかけで止まった(電源断など)
        again = self.me.preview(self.excel).get_json()
        self.assertFalse(again["cached"])
        self.assertEqual(self.image(again).status_code, 200)

    def test_控えの絵が消えていたら作り直す(self) -> None:
        data = self.me.preview(self.excel).get_json()
        meta = self.me.biz.preview_cache.get_meta(data["key"])
        os.remove(os.path.join(self.me.biz.preview_cache.dir, meta["file"]))
        self.assertEqual(self.image(data).status_code, 404)
        again = self.me.preview(self.excel).get_json()
        self.assertFalse(again["cached"])
        self.assertEqual(self.image(again).status_code, 200)

    def test_控えの中身が欠けていたら作り直す(self) -> None:
        """電源断・掃除ソフトなどで、控えの絵・PDF が0バイトや途中までになっていたら、
        その控えは出さずに作り直す(以前は壊れた控えを「控えの画像」として出し続けた。
        画像なら「もう一度」で作り直せるが、PDF の枠は壊れたまま何も言わない)。"""
        for item_id in (self.excel, self.word):
            data = self.me.preview(item_id).get_json()
            meta = self.me.biz.preview_cache.get_meta(data["key"])
            path = os.path.join(self.me.biz.preview_cache.dir, meta["file"])
            for broken in (b"", b"%PDF-1.4\n"):
                with open(path, "wb") as fp:
                    fp.write(broken)
                again = self.me.preview(item_id).get_json()
                self.assertFalse(again["cached"], f"欠けた控え({len(broken)} バイト)を出した")
                res = self.image(again)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(len(res.data), meta["bytes"])

    def test_控えのフォルダごと消されても作り直して続ける(self) -> None:
        self.me.preview(self.excel)
        shutil.rmtree(self.me.biz.preview_cache.dir)
        again = self.me.preview(self.excel).get_json()
        self.assertFalse(again["cached"])
        self.assertEqual(self.image(again).status_code, 200)

    def test_Wordを画像で頼んだときは別の控え(self) -> None:
        pdf = self.me.preview(self.word).get_json()
        img = self.me.preview(self.word, image=True).get_json()
        self.assertNotEqual(pdf["key"], img["key"])
        self.assertEqual((pdf["format"], img["format"]), ("pdf", "image"))
        self.assertFalse(img["cached"])


# ======================================================================
# 7. 拡張子(大文字・Word の古い形式・対象外)
# ======================================================================
class ExtensionFlowTests(_FlowCase):
    FILES = ["c/s/c_s_大文字.XLSX", "c/s/c_s_混ぜ書き.XlsM", "c/s/c_s_古いExcel.xls",
             "c/s/c_s_古いWord.doc", "c/s/c_s_大文字Word.DOCX", "c/s/c_s_マクロWord.docm",
             "c/s/c_s_資料.pdf", "c/s/c_s_一覧.csv", "c/s/c_s_メモ.txt", "c/s/c_s_バイナリ.xlsb",
             "c/s/c_s_ショートカット.xlsx.lnk", "c/s/c_s_控え.xlsx.bak", "c/s/c_s_拡張子なし"]

    def setUp(self) -> None:
        super().setUp()
        os.makedirs(self.path("c/s/c_s_フォルダ.xlsx"))  # 名前が .xlsx のフォルダ
        self.me = self.pc()

    def test_ExcelとWordだけを大文字小文字を問わず載せる(self) -> None:
        items = self.me.items()
        self.assertEqual(sorted(items), sorted([
            "c_s_大文字.XLSX", "c_s_混ぜ書き.XlsM", "c_s_古いExcel.xls",
            "c_s_古いWord.doc", "c_s_大文字Word.DOCX", "c_s_マクロWord.docm"]))
        self.assertEqual(items["c_s_大文字.XLSX"]["ext"], "xlsx")
        self.assertEqual(items["c_s_大文字Word.DOCX"]["ext"], "docx")

    def test_大文字の拡張子もWordの古い形式も_それぞれのアプリでプレビュー印刷する(self) -> None:
        items = self.me.items()
        expect = {"c_s_大文字.XLSX": "image", "c_s_混ぜ書き.XlsM": "image", "c_s_古いExcel.xls": "image",
                  "c_s_古いWord.doc": "pdf", "c_s_大文字Word.DOCX": "pdf", "c_s_マクロWord.docm": "pdf"}
        for name, fmt in expect.items():
            res = self.me.preview(items[name]["id"])
            self.assertEqual(res.status_code, 200, (name, res.get_json()))
            self.assertEqual(res.get_json()["format"], fmt, name)
        self.me.print([items["c_s_古いWord.doc"]["id"], items["c_s_大文字.XLSX"]["id"]])
        job = self.me.wait_print()
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["app"], "Excel・Word")
        self.assertEqual(job["failed"], 0)

    def test_設定の拡張子は大文字や点付きでもよい(self) -> None:
        self.me.biz.cfg.extensions = ["XLSX", ".Doc"]
        names = sorted(self.me.refresh() and self.me.items())
        self.assertEqual(names, ["c_s_古いWord.doc", "c_s_大文字.XLSX"])


if __name__ == "__main__":
    unittest.main()
