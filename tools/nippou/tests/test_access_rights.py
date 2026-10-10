"""アクセス権限 ── マスタの表から「このPCのライン」と「管理者か」を決める (v4.12.0〜v4.12.2)

    マスタ管理に テーブル：アクセス権限 があるので カラム：権限 にライン名が
    のっているのでそれをラインに設定する仕組み(パスワードがあれば現行の通り変更可)
    カラム：権限 に Administrator ライン名(L-1、HVC...etc) それ以外の文字列はスルー

【現物の形(v4.12.1)】 管理番号 / ログインID / PC名 / 権限 / 有効 / 備考 ── 現場の
各ラインのPCは**同じ1つのログインID**で、PCごとに行がある。1つのPCに行が2つ以上
あることも普通(ライン名の行と mode:field の行)。下の値はどれも架空です。

【約束】
    ・このPCの行 = **ログインIDとPC名の両方**が同じ行(空の欄は問わない)
      どちらの列も無い表は、どれかの欄がログイン名かPC名と同じ行
    ・有効が 0 の行は読まない
    ・権限: Administrator → 管理者 / **正規の呼び名**(`logic/line_names`: L-1・機側・
      中板3 → 中板 設備3)→ ライン / それ以外は読まない(L1 のような前の名前・正規でない
      書き方も。読まなかったことと正規の書き方を見せる)
    ・ツールの名前も正規の呼び名(v4.13.0)。v4.12 までに当てた印(`MARU:4`)は正規へ揃えて比べる
    ・ラインが2つ以上なら決めない(理由を言う)。読まなかった権限は並べて見せる
    ・ラインは**表の値が変わったときだけ**当てる(パスワードで変えたラインを戻さない)
    ・Administrator は記録を直せる(`ctx.editor`)が、ラインを変えるのは管理者モードだけ
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nippou.logic import access_rights as logic  # noqa: E402
from tests._web import WebTestCase  # noqa: E402

#: 現場の共通アカウント(架空)と、PC(架空)
FLOOR = "fac-floor"
PCS = {"HVC": "FAC-PC-000101", "L-1": "FAC-PC-000102", "LVC": "FAC-PC-000103",
       "機側": "FAC-PC-000104", "NS1": "FAC-PC-000105", "作業長": "FAC-PC-000106",
       "コイル": "FAC-PC-000107"}
ADMIN = ("fac-admin01", "OFFICEPC01")


def table(*extra) -> list[dict]:
    """現物と同じ形の表: 共通アカウント × PC ごとに、ライン名の行と mode:field の行。"""
    out, n = [], 0
    for rights, pc in PCS.items():
        for value in (rights, "mode:field"):
            n += 1
            out.append({"管理番号": n, "ログインID": FLOOR, "PC名": pc, "権限": value,
                        "有効": 1, "備考": ""})
    for value in ("mode:field", "mode:material", "Administrator"):
        n += 1
        out.append({"管理番号": n, "ログインID": ADMIN[0], "PC名": ADMIN[1], "権限": value,
                    "有効": 1, "備考": None})
    return out + list(extra)


def on(pc: str, login: str = FLOOR) -> logic.Identity:
    return logic.Identity(login=login, pc=pc)


class LogicTests(unittest.TestCase):
    def test_読むのは正規の呼び名だけ(self) -> None:
        self.assertEqual(logic.line_of("L-1"), "L-1")
        self.assertEqual(logic.line_of("機側"), "機側")           # 定義の表(ライン毎目標と同じ)
        self.assertEqual(logic.line_of("ﾄｯﾄ"), "トット")          # 文字の幅だけは揃える
        for text in ("L1", "l-1", "ｌ―１", "L 1", "hvc", "LS", "L-9", "", "コイル", "作業長",
                     "mode:field"):
            self.assertIsNone(logic.line_of(text), text)

    def test_正規でない書き方は読まず_正規を添える(self) -> None:
        rows = table()
        for r in rows:
            if r["PC名"] == PCS["L-1"] and r["権限"] == "L-1":
                r["権限"] = "L1"                                   # 前の名前(VBA)で書いてしまった
        got = logic.decide(rows, on(PCS["L-1"]))
        self.assertIsNone(got.line)
        self.assertEqual(got.ignored, ("L1(正規は「L-1」)", "mode:field"))
        self.assertIn("読まなかった権限: L1(正規は「L-1」)", got.summary())

    def test_共通アカウントでも_PCごとに自分のラインだけ(self) -> None:
        """v4.12.0 は「ログイン名**か**PC名」で当てたので、共通アカウントの行が
        全部当たり、どのPCも「ラインが5つ → 決めない」になっていた。"""
        expected = {"HVC": "HVC", "L-1": "L-1", "LVC": "LVC", "機側": "機側", "NS1": "NS1"}
        # (コイルはこのツールに無いライン ── 下の試験)
        for rights, line in expected.items():
            got = logic.decide(table(), on(PCS[rights]))
            self.assertEqual((got.line, got.admin, got.problem), (line, False, ""), rights)
            self.assertEqual(len(got.rows), 2)                     # ライン名の行と mode:field
            self.assertEqual(got.rows[0].matched_by, "ログインID・PC名")
            self.assertEqual(got.ignored, ("mode:field",))
        self.assertEqual(logic.decide(table(), on(PCS["L-1"])).summary(),
                         "ライン L-1(読まなかった権限: mode:field)")
        self.assertEqual(logic.decide(table(), on(PCS["機側"])).summary(),
                         "ライン 機側(読まなかった権限: mode:field)")

    def test_ラインを書いていないPCは決めない_読まなかった権限を見せる(self) -> None:
        for rights in ("作業長", "コイル"):
            got = logic.decide(table(), on(PCS[rights]))
            self.assertIsNone(got.line)
            self.assertEqual(got.problem, "")
            self.assertEqual(got.ignored, (rights, "mode:field"))
            self.assertIn(f"読まなかった権限: {rights}", got.summary())

    def test_中板はラインNOとセットで_設備番号(self) -> None:
        pc = "FAC-PC-000108"
        base = {"管理番号": 50, "ログインID": FLOOR, "PC名": pc, "有効": 1, "備考": ""}
        got = logic.decide(table({**base, "権限": "中板3"}), on(pc))
        self.assertEqual((got.line, got.number, got.applied_key), ("中板", "3", "中板:3"))
        self.assertEqual(got.summary(), "ライン 中板3")
        # 書き方は「中板3」だけ(空白・記号を挟んだものは読まない)
        got = logic.decide(table({**base, "権限": "中板 3"}), on(pc))
        self.assertIsNone(got.line)
        self.assertIn("中板 3(中板3 のように続けて書きます)", got.summary())
        # ラインNO が無ければ決めない(どの設備か分からない)
        got = logic.decide(table({**base, "権限": "中板"}), on(pc))
        self.assertIsNone(got.line)
        self.assertIn("ラインNO(1〜7)とセット", got.problem)
        self.assertIn("ラインは決めません", got.problem)

    def test_Administrator(self) -> None:
        got = logic.decide(table(), on(ADMIN[1], ADMIN[0]))
        self.assertEqual((got.admin, got.line), (True, None))     # ラインは触らない
        self.assertEqual(got.summary(),
                         "Administrator(読まなかった権限: mode:field・mode:material)")
        # 同じPCでも、ほかのログインIDでは管理者にならない
        self.assertFalse(logic.decide(table(), on(ADMIN[1], FLOOR)).admin)
        # 1つの欄に並べてもよい
        got = logic.decide([{"ログインID": FLOOR, "PC名": PCS["HVC"],
                             "権限": "administrator, HVC"}], on(PCS["HVC"]))
        self.assertEqual((got.admin, got.line), (True, "HVC"))

    def test_書き方の揺れ_大文字小文字_全角_DOMAIN(self) -> None:
        got = logic.decide(table(), on(PCS["HVC"].lower(), FLOOR.upper()))
        self.assertEqual(got.line, "HVC")
        got = logic.decide(table(), on("ＦＡＣ－ＰＣ－０００１０１"))
        self.assertEqual(got.line, "HVC")
        got = logic.decide([{"ログインID": f"CORP\\{FLOOR}", "PC名": PCS["LVC"], "権限": "LVC"}],
                           on(PCS["LVC"]))
        self.assertEqual(got.line, "LVC")

    def test_空の欄は問わない(self) -> None:
        rows = [{"ログインID": "", "PC名": PCS["NS1"], "権限": "NS1"},     # そのPCなら誰でも
                {"ログインID": ADMIN[0], "PC名": "", "権限": "Administrator"},  # どのPCでも
                {"ログインID": "", "PC名": "", "権限": "HVC"}]            # 誰の行でもない
        self.assertEqual(logic.decide(rows, on(PCS["NS1"], "someone")).line, "NS1")
        self.assertTrue(logic.decide(rows, on("ANYPC", ADMIN[0])).admin)
        self.assertIn("このPC", logic.decide(rows, on("ANYPC", "someone")).problem)

    def test_有効が0の行は読まない(self) -> None:
        rows = table()
        for r in rows:
            if r["PC名"] == PCS["HVC"]:
                r["有効"] = 0
        self.assertIn("このPC", logic.decide(rows, on(PCS["HVC"])).problem)
        for off in (0, "0", "False", "いいえ", "無効"):
            self.assertFalse(logic.is_valid(off), off)
        for on_ in (1, "1", "True", "はい", "", None):
            self.assertTrue(logic.is_valid(on_), on_)

    def test_ラインが2つなら決めない(self) -> None:
        extra = {"管理番号": 99, "ログインID": FLOOR, "PC名": PCS["HVC"], "権限": "L-1", "有効": 1}
        got = logic.decide(table(extra), on(PCS["HVC"]))
        self.assertIsNone(got.line)
        self.assertEqual(got.lines, ("HVC", "L-1"))
        self.assertIn("自動では決めません", got.problem)

    def test_列の名前が分からない表は_どれかの欄で当てる(self) -> None:
        rows = [{"担当者": "suzuki", "権限": "HVC"}, {"担当者": FLOOR, "権限": "L-1"}]
        got = logic.decide(rows, on("X"))
        self.assertEqual(got.line, "L-1")
        self.assertEqual(got.rows[0].matched_by, "担当者")

    def test_行が無い_列が無い(self) -> None:
        self.assertIn("このPC", logic.decide(table(), on("UNKNOWN-PC")).problem)
        self.assertIn("「権限」の列", logic.decide([{"ログインID": FLOOR, "ライン": "L1"}],
                                                   on("X")).problem)
        self.assertIn("行がありません", logic.decide([], on("X")).problem)

    # -- 間違いに気づく(表ぜんぶの点検) ----------------------------------
    def test_点検_現物の形なら何も言わない(self) -> None:
        self.assertEqual(logic.inspect(table()), [])

    def test_点検_ほかのPCの行の書き間違いも見つける(self) -> None:
        rows = table()
        rows[2]["権限"] = "L1"                                  # 管理番号 3 = L-1 のPCの行
        rows[6]["権限"] = "中板 3"                              # 管理番号 7
        rows.append({"管理番号": 90, "ログインID": "", "PC名": "", "権限": "HVC", "有効": 1})
        rows.append({"管理番号": 91, "ログインID": FLOOR, "PC名": PCS["HVC"], "権限": "LVC",
                     "有効": 1})
        rows.append({"管理番号": 92, "ログインID": FLOOR, "PC名": PCS["NS1"], "権限": "L1",
                     "有効": 0})                                # 有効でなくても書き間違いは言う
        got = [f.describe() for f in logic.inspect(rows)]
        self.assertEqual(got, [
            "管理番号 1: 同じログインID・PC名にラインが2つあります(HVC・LVC、"
            "管理番号 1・管理番号 91)── どちらか分からないので決めません",
            "管理番号 3: 権限: 「L1」は読みません(正規は「L-1」)",
            "管理番号 7: 権限: 「中板 3」は読みません(中板は「中板3」のように"
            "ラインNO を続けて書きます)",
            "管理番号 90: ログインIDもPC名も空なので、どのPCの行にもなりません",
            "管理番号 92: 権限: 「L1」は読みません(正規は「L-1」)",
        ])

    def test_当てるのは表の値が変わったときだけ(self) -> None:
        self.assertTrue(logic.should_apply("L-1", ""))
        self.assertFalse(logic.should_apply("L-1", "L-1"))   # 前と同じ → パスワードで変えたものを残す
        self.assertTrue(logic.should_apply("HVC", "L-1"))    # 表を書き換えた
        self.assertFalse(logic.should_apply(None, "L-1"))


class AccessRightsWebTests(WebTestCase):
    terminal_line = None
    #: このPC(L-1 のPCに、現場の共通アカウントで入っている)
    me = on(PCS["L-1"])

    def setUp(self) -> None:
        super().setUp()
        from nippou.services import access_rights
        self.svc = access_rights
        access_rights.reset()
        patcher = patch.object(access_rights, "identity", side_effect=lambda: self.me)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(access_rights.reset)

    def master(self, rows: list[dict], *, name="アクセス権限", file="material") -> Path:
        """現物と同じ形の表を、梱包資材マスタ(現物の置き場所)に作る。"""
        from nippou import source_db
        from nippou.config import SETTINGS

        path = (SETTINGS.gw_material_master_path if file == "material"
                else SETTINGS.transmission_master_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.execute(f'CREATE TABLE "{name}" (管理番号 INTEGER, ログインID TEXT, PC名 TEXT,'
                     ' 権限 TEXT, 有効 INTEGER, 備考 TEXT)')
        conn.executemany(f'INSERT INTO "{name}" VALUES (:管理番号, :ログインID, :PC名, :権限,'
                         ' :有効, :備考)', rows)
        conn.commit()
        conn.close()
        source_db.forget(path)                       # 写しを捨てて読み直させる
        return path

    def test_起動で表のラインを当てる(self) -> None:
        from nippou import config, user_settings, work_context

        self.master(table())
        status = self.svc.apply()
        ctx = work_context.get_context()
        self.assertEqual((ctx.line, ctx.terminal_line), ("L-1", "L-1"))
        self.assertTrue(work_context.terminal_line_decided())
        self.assertEqual(user_settings.get(config.KEY_ACCESS_LINE_APPLIED), "L-1")
        self.assertIn("梱包資材マスタ の アクセス権限", status.applied)

    def test_機側のPCは_機側(self) -> None:
        from nippou import work_context

        self.me = on(PCS["機側"])
        self.master(table())
        self.svc.apply()
        self.assertEqual(work_context.get_context().terminal_line, "機側")

    def test_中板のPCは_中板と設備番号(self) -> None:
        from nippou import config, user_settings, work_context

        self.me = on("FAC-PC-000108")
        self.master(table({"管理番号": 50, "ログインID": FLOOR, "PC名": "FAC-PC-000108",
                           "権限": "中板4", "有効": 1, "備考": ""}))
        status = self.svc.apply()
        ctx = work_context.get_context()
        self.assertEqual((ctx.terminal_line, ctx.terminal_maru_sub), ("中板", "4"))
        self.assertEqual(user_settings.get(config.KEY_ACCESS_LINE_APPLIED), "中板:4")
        self.assertIn("中板4 にしました", status.applied)

    def test_v412までに当てた印は正規へ揃えて比べる(self) -> None:
        """`MARU:4` のまま比べると「表の値が変わった」に見え、パスワードで変えたラインを
        表の値へ戻してしまう。"""
        from nippou import config, user_settings, work_context

        self.me = on("FAC-PC-000108")
        self.master(table({"管理番号": 50, "ログインID": FLOOR, "PC名": "FAC-PC-000108",
                           "権限": "中板4", "有効": 1, "備考": ""}))
        user_settings.save_many({config.KEY_ACCESS_LINE_APPLIED: "MARU:4",
                                 config.KEY_TERMINAL_LINE: "LS"})   # パスワードで機側に変えてあった
        work_context.reset()
        status = self.svc.apply()
        self.assertEqual(status.applied, "")
        self.assertEqual(work_context.get_context().terminal_line, "機側")   # 読むときに正規へ

    def test_表の点検が設定の画面に出る(self) -> None:
        rows = table()
        rows[6]["権限"] = "LS"                                  # 機側のPCの行を前の名前で書いた
        self.master(rows)
        self.svc.load()
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="access-findings"', page)
        self.assertIn("読めない行が1行あります", page)
        self.assertIn("管理番号 7: 権限: 「LS」は読みません(正規は「機側」)", page)
        self.master(table())
        self.svc.load()
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn(f"表の点検: {len(table())}行を確かめました ── 読めない行はありません", page)

    def test_マスタ管理_読めない値は保存の前に断る_ラインでない値は通す(self) -> None:
        self.master(table())
        self.post("/api/master/unlock", {"enable": True, "password": "nisk"})
        base = {"file": "material", "table": "アクセス権限"}
        res = self.post("/api/master/row/save", {**base, "key": 3, "values": {"権限": "L1"}})
        self.assertEqual(res.status_code, 400)                  # マスタ管理の「値の形が違う」
        self.assertEqual(res.get_json()["error"]["code"], "bad_value")
        self.assertIn("「L1」は読みません(正規は「L-1」)", res.get_json()["error"]["message"])
        res = self.post("/api/master/row/add", {**base, "values": {
            "管理番号": 80, "ログインID": FLOOR, "PC名": "FAC-PC-000109", "権限": "中板 3",
            "有効": 1}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("中板3", res.get_json()["error"]["message"])
        # ラインでない値・正規の値は通す。直したらこのPCの判定も言う
        res = self.post("/api/master/row/save", {**base, "key": 4, "values": {"権限": "作業長"}})
        self.assertEqual(res.status_code, 200, res.get_json())
        res = self.post("/api/master/row/save", {**base, "key": 3, "values": {"権限": "中板2"}})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("このPCの判定: ライン 中板2", res.get_json()["message"])

    def test_マスタ管理の表に点検が出る(self) -> None:
        rows = table()
        rows[2]["権限"] = "L1"
        self.master(rows)
        body = self.get("/api/master/browse?file=material&table=アクセス権限").get_json()
        self.assertEqual(body["page"]["checks"],
                         ["管理番号 3: 権限: 「L1」は読みません(正規は「L-1」)"])
        # ほかの表には出さない
        body = self.get("/api/master/browse?file=material&table=班員名簿").get_json()
        self.assertEqual(body["page"].get("checks", []), [])

    def test_ラインが決まらない端末は_日報入力でその理由を言う(self) -> None:
        rows = table()
        rows[2]["権限"] = "L1"                                  # このPC(L-1)の行
        self.master(rows)
        self.svc.apply()
        note = self.post("/api/entry/state", {}).get_json()["line_note"]
        self.assertIn("アクセス権限の表では決まりませんでした", note)
        self.assertIn("L1(正規は「L-1」)", note)

    def test_この端末のラインの札は正規の呼び名で_どこで決まったかも(self) -> None:
        import re

        self.me = on(PCS["機側"])
        self.master(table())
        self.svc.apply()
        page = self.get("/settings?tab=terminal").get_data(as_text=True)
        self.assertIn('<b class="line-now">機側</b>', page)
        self.assertNotIn("<code>LS</code>", page)                  # 前の名前は添えない(v4.13.0)
        self.assertIn("アクセス権限の表で決まりました ── この端末に覚えています", page)
        # ボタンは定義の表の順・正規の呼び名(値も正規の呼び名)
        labels = re.findall(r'data-line="([^"]+)" data-label="([^"]+)"', page)
        self.assertEqual(labels, [("L-1", "L-1"), ("LVC", "LVC"), ("HVC", "HVC"), ("機側", "機側"),
                                  ("NS1", "NS1"), ("AIM", "AIM"), ("トット", "トット"),
                                  ("バランサー", "バランサー"), ("中板", "中板")])
        self.assertNotIn('line-choices__code', page)
        # 管理者モードで選んだら、そう言う
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        self.post("/api/entry/line", {"line": "中板", "maru_sub": "4"})
        page = self.get("/settings?tab=terminal").get_data(as_text=True)
        self.assertIn('<b class="line-now">中板4</b>', page)
        self.assertIn("管理者モードで選びました ── この端末に覚えています", page)
        self.assertIn(">中板4</button>", page)                 # 中板のラインNO のボタン

    def test_伝送用ファイルにあればそちらを先に読む(self) -> None:
        self.master(table())
        self.master([{"管理番号": 1, "ログインID": FLOOR, "PC名": PCS["L-1"], "権限": "LVC",
                      "有効": 1, "備考": ""}], file="transmission")
        status = self.svc.apply()
        self.assertEqual(status.decision.line, "LVC")
        self.assertIn("伝送用ファイル", status.source)

    def test_このPCの行がある表を先に読む(self) -> None:
        """伝送用ファイルにも「アクセス権限」があるが、このPCの行は梱包資材マスタにだけある。

        前は最初に見つかった伝送用ファイルの表だけを読み、梱包資材マスタに足した
        mode:fullaccess が効かなかった。
        """
        self.master([{"管理番号": 1, "ログインID": FLOOR, "PC名": "FAC-PC-999999", "権限": "HVC",
                      "有効": 1, "備考": ""}], file="transmission")
        self.master(table({"管理番号": 90, "ログインID": FLOOR, "PC名": PCS["L-1"],
                           "権限": "mode:fullaccess", "有効": 1, "備考": ""}))
        status = self.svc.load()
        self.assertIn("梱包資材マスタ", status.source)
        self.assertTrue(status.decision.full_access)

    def test_マスタ管理でmode_fullaccessを足すと左のタブにすぐ効く(self) -> None:
        self.master(table())
        self.svc.load()
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn("<li data-nav-full hidden>", page, "出さないタブは描いて隠す")
        self.post("/api/master/unlock", {"enable": True, "password": "nisk"})
        res = self.post("/api/master/row/add", {"file": "material", "table": "アクセス権限", "values": {
            "管理番号": 90, "ログインID": FLOOR, "PC名": PCS["L-1"], "権限": "mode:fullaccess",
            "有効": 1}})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertIs(body["all_tabs"], True, "画面がその場でレールを出し入れする")
        self.assertIn("左のタブ: 1〜8 全部", body["message"])
        page = self.get("/settings").get_data(as_text=True)
        self.assertNotIn("<li data-nav-full hidden>", page)

    def test_パスワードで変えたラインは表が変わるまで残る(self) -> None:
        from nippou import work_context

        self.master(table())
        self.svc.apply()
        # 管理者モードで LVC に変えた
        self.post("/api/settings/admin", {"enable": True, "password": "nisk"})
        res = self.post("/api/entry/line", {"line": "LVC"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.svc.apply()                             # 起動し直した(表は同じ)
        self.assertEqual(work_context.get_context().terminal_line, "LVC")
        rows = table()
        for r in rows:                               # 表を書き換えた(このPCを HVC に)
            if r["PC名"] == PCS["L-1"] and r["権限"] == "L-1":
                r["権限"] = "HVC"
        self.master(rows)
        self.svc.apply()
        self.assertEqual(work_context.get_context().terminal_line, "HVC")

    def test_表が無ければ何もしない(self) -> None:
        from nippou import work_context

        status = self.svc.apply()
        self.assertFalse(work_context.terminal_line_decided())
        self.assertIn("アクセス権限", status.summary())
        self.master(table(), name="別の表")
        self.assertIn("表がありません", self.svc.apply().summary())

    def test_Administrator_は記録を直せるが_ラインは変えられない(self) -> None:
        from nippou import work_context

        self.me = on(ADMIN[1], ADMIN[0])
        self.master(table())
        self.svc.apply()
        ctx = work_context.get_context()
        self.assertTrue(self.svc.is_administrator())
        self.assertTrue(ctx.editor)
        self.assertFalse(ctx.admin)
        res = self.post("/api/entry/line", {"line": "LVC"})
        self.assertEqual(res.status_code, 403)       # ラインを変えるのは管理者モードだけ

    def test_設定の画面に出る_読み直す(self) -> None:
        self.master(table())
        page = self.get("/settings").get_data(as_text=True)
        self.assertIn('id="access-rights"', page)
        self.assertIn(f"ログイン名 {FLOOR} / PC名 {PCS['L-1']}", page)
        body = self.post("/api/settings/access-rights", {}).get_json()
        self.assertEqual(body["line"], "L-1")
        self.assertEqual(body["line_now"], "L-1")
        self.assertIn("L-1 にしました", body["message"])
        # 正規の書き方の表も画面に出す(前の名前は VBA の欄)
        self.assertIn('id="line-names"', page)
        self.assertIn("<td>機側</td><td>LS</td>", page)
        self.assertIn("<td>中板1〜中板7(ラインNO を続けて)</td><td>MARU</td>", page)
        body = self.post("/api/settings/access-rights", {}).get_json()
        # 2回目は当て直さない。読まなかった権限も見せる
        self.assertEqual(body["message"], "アクセス権限: ライン L-1(読まなかった権限: mode:field)")


if __name__ == "__main__":
    unittest.main()
