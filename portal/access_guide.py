"""アクセス権限の早見表 ── 「権限」の欄に何を書くと、どのツールで何が出るか(大設定に出す)

梱包資材マスタの「アクセス権限」表は、python-web-tools(梱包資材総合ツール)と、日報・看板・
カレンダーが**同じ表を別々の決まりで**読みます。1 つの値がツールによって効いたり読まれなかったり
するので、書く人が結果を説明できるよう、4 ツールぶんを 1 枚にまとめて大設定に出します。

**ここは説明だけで、判断はしません。** 判断は各ツールの中にあります:

    日報        tools/nippou/nippou/logic/access_rights.py(と services/access_rights.py)
    看板        tools/kanban/kanban/access_control.py ・ kanban/line_names.py
    カレンダー  tools/calendar/calendar_app/access_control.py ・ calendar_app/config.py
    点検表      アクセス権限を読まない

説明と各ツールの中身が食い違わないよう、`tests/test_access_guide.py` が各ツールの値(読む権限・
ライン名の一覧)と突き合わせます。ツールの決まりを変えたら、ここも直さないと試験が落ちます。

どのタブを出すかは、この表ではなく共有の DB の「タブ表示権限」で決めます(大設定のもう 1 つの表)。
"""
from __future__ import annotations

#: 列に並べるツール(大きなタブの並び)
TOOLS: tuple[tuple[str, str], ...] = (
    ("nippou", "日報"), ("kanban", "看板"), ("calendar", "カレンダー"), ("inspection", "点検表"),
)

#: 読まない(効かない)ときの字
NOT_READ = "読まない"

#: 各ツールが**ライン**として読む名前(正規の呼び名)。ここに無い書き方(L1・LS・l-1 など)は読まない
LINE_NAMES: dict[str, tuple[str, ...]] = {
    "nippou": ("L-1", "LVC", "HVC", "トット", "バランサー", "AIM", "機側", "NS1", "中板"),
    "kanban": ("L-1", "LVC", "HVC", "機側", "AIM", "コイル", "トット", "バランサー", "NS1"),
    "calendar": ("コイル", "LVC", "HVC", "機側", "L-1", "AIM", "NS1", "大板小板", "作業長"),
}

#: 値ごとに、各ツールで何が起きるか。(値, 説明, {ツール: 起きること})
ROWS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("Administrator", "管理者", {
        "nippou": "どのラインの日報も「記録を見る」で見て直せる。左のタブを 1〜8 全部出す",
        "kanban": "現場モード・倉庫モードのどちらも使える(切り替えられる)",
        "calendar": NOT_READ,
        "inspection": NOT_READ,
    }),
    ("mode:fullaccess", "日報の左のタブを全部", {
        "nippou": "左のタブを 1〜8 全部出す(無い PC は 1・2・3・5・8 だけ)",
        "kanban": NOT_READ,
        "calendar": NOT_READ,
        "inspection": NOT_READ,
    }),
    ("mode:field", "現場モード", {
        "nippou": NOT_READ,
        "kanban": "現場モードを使える",
        "calendar": NOT_READ,
        "inspection": NOT_READ,
    }),
    ("mode:material", "資材(倉庫)モード", {
        "nippou": NOT_READ,
        "kanban": "倉庫モードを使える(python-web-tools では資材モード)",
        "calendar": NOT_READ,
        "inspection": NOT_READ,
    }),
    ("ライン名(例: L-1・HVC・機側)", "下の「ライン名の一覧」", {
        "nippou": "この端末のラインにする(1 つだけのとき。中板は 中板3 のようにラインNOを続ける)",
        "kanban": "担当ラインにする(1 つだけのとき)",
        "calendar": "使えるラインにする(1 つなら固定・複数ならその中で選ぶ。切り替えは管理者パスワード)",
        "inspection": NOT_READ,
    }),
)

#: 当てはまる行が 1 つも無い端末
NO_ROW: dict[str, str] = {
    "nippou": "ラインは設定・管理者で選ぶ。左のタブは 1・2・3・5・8 だけ(表を読めないときは全部)",
    "kanban": "現場モードだけ",
    "calendar": "管理者パスワードで、どのラインにも切り替えられる",
    "inspection": NOT_READ,
}

#: 4 ツール(と python-web-tools)で同じ読み方
COMMON_RULES: tuple[str, ...] = (
    "1 行 = 1 つの許可。ログインID と PC名 が当てはまる行の権限を全部使う(和)",
    "空欄は「問わない」。ログインID も PC名 も空の行は、誰にも効かない(全員への許可になるため)",
    "「有効」が 0 の行は読まない(空欄は有効)",
    "ログインID・PC名 の大文字小文字は問わない",
    "このツールの知らない値(ほかのツールの権限)は、消さずにそのまま置いておく",
    "日報・看板: 1 つの欄に、で並べて書いてもよい(例: L-1、mode:field)。Administrator の大文字小文字・全角半角は問わない",
    "カレンダー: 1 行にライン名を 1 つだけ。並べて書いた欄や、字の違う書き方(ｺｲﾙ など)は読まない",
)


def view() -> dict:
    """画面に渡す形。"""
    return {
        "tools": [{"id": tid, "title": title} for tid, title in TOOLS],
        "rows": [{"value": value, "note": note, "cells": [cells.get(tid, NOT_READ) for tid, _ in TOOLS]}
                 for value, note, cells in ROWS],
        "no_row": [NO_ROW.get(tid, NOT_READ) for tid, _ in TOOLS],
        "lines": [{"title": title, "names": list(LINE_NAMES.get(tid, ()))}
                  for tid, title in TOOLS if tid in LINE_NAMES],
        "rules": list(COMMON_RULES),
        "not_read": NOT_READ,
    }
