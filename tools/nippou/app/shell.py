"""画面の外枠に渡す値

左のレールの並びと、テンプレートが必要とする共通の値をここで作る。
**並びは作業の順序**で、tkinter版のボタンの並び(さらに遡ると VBA の
`UFdaily` の操作順)と同じ。番号は装飾ではなく、順序が実在するから
振っている。

ラインの選択は日報入力画面の中で行い、どのラインでも出せる画面は同じ。
ただし**アクセス権限の表に `mode:fullaccess` が無い PC は、左のタブを
1・2・3・5・8 だけ**にする(v4.25.0。`LIMITED_NAV`)。番号は元のまま出す
(説明書・電話での「4番の記録を見る」と食い違わないように)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from flask import current_app

from nippou import app_config, idle_exit


@dataclass
class NavItem:
    key: str
    label: str
    url: str
    note: str = ""                  # 名前の下に出す一行。何をする画面か
    no: int = 0                     # レールの番号(1〜8。絞っても元の番号のまま)
    badge: str = ""
    badge_kind: str = "todo"        # "todo" | "done" | "alert"
    ready: bool = True              # まだ作っていない画面は False
    #: mode:fullaccess の PC にだけ出すタブ(4・6・7)
    full_only: bool = False
    #: いまは出さない(このPCに mode:fullaccess が無い)。**描くが隠す** ── マスタ管理で
    #: アクセス権限を直したとき、画面を読み直さずにその場で出し入れするため
    hidden: bool = False


# 左のレール。左から作業する順:
#   入力して保存する → 計算する → 見る → 整える
#
# 【名前は「動詞」でそろえる】
# VBA はボタン名が物(「日報DB」「graphF」)で、**押すと何が起きるか**が
# 名前から読めませんでした。ここは
#
#     保存する / 呼び出す / 見る(紙・表・グラフ) / 共有へ保存する
#
# の4つが日々の仕事なので、レールの名前と説明をその4つに当てます。
# 名前だけでは足りないものには一行の説明を添える(文言はサーバが持つ ──
# 画面が言葉を組み立てると、同じことを2か所で決めることになる)。
#
# 【「記録」を独立させた理由】
# 「過去の直を開く」と「保存したものを見る」が**設定・管理者の中**に
# 埋まっていました。日々使うものが、めったに触らない設定の画面に同居して
# いると探せません。見る・呼び出すをまとめて1枚にし、設定には環境を
# 整えるものだけを残します。
NAV: tuple[tuple[str, str, str, str], ...] = (
    ("entry", "日報入力", "/", "打って、この端末に保存する"),
    ("gw", "梱包資材重量計算", "/gw", "資材の重量を出す"),
    ("vc", "VC長さ計算", "/vc", "VCフィルムの残り長さ・枚数・早見表"),
    ("records", "記録を見る", "/records", "紙を見る・過去の直を呼び出す"),
    ("graph", "集計・グラフ", "/graph", "グラフで見る(直別・期間の推移)"),
    ("agg", "集計管理", "/agg", "表で見る(ロット別・直別・日別)"),
    ("standard", "標準作業時間", "/standard-time", "同じ条件の作業時間を見る・比べる"),
    ("settings", "設定・管理者", "/settings", "共有へ保存する・環境を整える"),
)

# mode:fullaccess の無い PC に出すタブ(v4.25.0):
#   1 日報入力 / 2 梱包資材重量計算 / 3 VC長さ計算 / 5 集計・グラフ / 8 設定・管理者
# 4 記録を見る・6 集計管理・7 標準作業時間 はレールに出さない(画面そのものは消さない ──
# 日報入力の「前の直を呼び出す」などの道からは今までどおり開ける)
LIMITED_NAV = frozenset({"entry", "gw", "vc", "graph", "settings"})

# もう作った画面。ここに挙がっていないものは「準備中」ページを出す。
#
# 【なぜ空振りさせないのか】
# レールに並べておいて404になると、押した人には「壊れている」と
# しか見えない。まだ無いことと壊れていることは違うので、**無いなら無いと
# 言う画面**を出す(黙って何も起きない状態を作らない)。
READY_SCREENS = frozenset({"entry", "gw", "vc", "graph", "agg", "standard", "records",
                           "settings"})

# 準備中の画面の説明。「いつ」と「それまでどうするか」まで書く。
#   key: (作る予定, 何ができるようになるか, いま使う画面)
PENDING_SCREENS: dict[str, tuple[str, str, str]] = {}

# 入口。レールの先頭(=作業の出発点)
HOME_URL = "/"
FALLBACK_URL = "/settings"


def nav_items(badges: Optional[dict[str, tuple[str, str]]] = None) -> list[NavItem]:
    """レールの項目。`badges` は `{key: (文言, 種別)}`。

    バッジは「その先に何があるか」を行く前に示すためのもの(共有へ未保存の件数
    など)。まだ作っていない画面には、行く前に分かるよう「準備中」を出す。
    """
    badges = badges or {}
    items = []
    every = _all_tabs()
    for no, (key, label, url, note) in enumerate(NAV, 1):
        full_only = key not in LIMITED_NAV
        ready = key in READY_SCREENS
        badge, kind = badges.get(key, ("", "todo"))
        if not ready and not badge:
            badge, kind = "準備中", "todo"
        items.append(NavItem(key=key, label=label, url=url, note=note, no=no,
                             badge=badge, badge_kind=kind, ready=ready,
                             full_only=full_only, hidden=full_only and not every))
    return items


def _all_tabs() -> bool:
    """左のタブを全部出すか(アクセス権限の mode:fullaccess。読めなければ全部)。"""
    try:
        from nippou.services import access_rights

        return access_rights.all_tabs()
    except Exception:                             # noqa: BLE001 - レールは必ず出す
        return True


def pending_items() -> list[NavItem]:
    """まだ作っていない画面。準備中ページの登録に使う。"""
    return [item for item in nav_items() if not item.ready]


def home_url() -> str:
    """`/` を開いたときに入る画面。

    レールの先頭(=作業の出発点)にする。ただし登録されていない場合が
    あるので、**実際に居るURL**から選ぶ。起動した先が404では、アプリが
    立ち上がったのかどうか分からない。
    """
    registered = {rule.rule for rule in current_app.url_map.iter_rules()}
    for item in nav_items():
        if item.ready and not item.hidden and item.url in registered:
            return item.url
    return FALLBACK_URL


# タブのアイコン。日報ツールは緑・文字は「日」。同じ現場で梱包資材ツール
# (紺)と並べて開くので、**タブの色と文字で取り違えない**ようにする
# (タブの表題は幅が足りず途中で切れる)
_FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="6" fill="#2f7d5f"/>'
            '<text x="16" y="23" font-size="20" font-family="sans-serif"'
            ' text-anchor="middle" fill="#ffffff">日</text></svg>')


def favicon() -> str:
    """タブのアイコン(SVGそのもの)。`data:` URL に埋めて使う。"""
    return _FAVICON


def save_badges() -> dict[str, tuple[str, str]]:
    """レールに出す「共有へ未保存」の件数。

    **2つある保存のうち、忘れられるのは共有のほうです。** 手元への保存は
    押すたび・自動でも走りますが、共有へ渡すのは直の終わりに1回だけの
    操作なので、やったかどうかが画面に出ていないと分かりません。
    どの画面に居ても数が見えるように、レールのバッジで出します。

    数えられなければ黙ります(バッジが出ないだけ)── 数えるために
    画面が出せなくなるのは本末転倒です。
    """
    from . import get_repo

    try:
        headers = get_repo().pending_sync_headers()
    except Exception:                             # noqa: BLE001 - 画面は出す
        return {}
    # **数えるのは直。** 押すのは直の単位(`shift_check.run_pending` が
    # 直でまとめる)なので、ページで数えると「4」と出て4回押す気になります
    pending = len({(h.report_date, h.line, h.shift) for h in headers})
    if not pending:
        return {}
    # **「未保存 3直」と書いてはいけません。**
    #
    # 2つの読み違いが起きました:
    #   1. 「3直」が**第3直**に読める(「3直の保存が無い」)
    #   2. 「未保存」が「手元にも残っていない」に読める ── 実際には
    #      手元には保存済みで、**共有へ渡していない**だけ
    #
    # 「ぶん」を付けて数だと分かるようにし、「未保存」ではなく
    # 「共有へ未送信」と、どこへ行っていないのかを書きます
    return {"settings": (f"共有へ未送信 {pending}直ぶん", "alert")}


def shell_context(active: str, *,
                  badges: Optional[dict[str, tuple[str, str]]] = None,
                  ribbon: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """`base.html` が必要とする値一式。

    各画面のルートは `render_template("...", **shell_context("entry"))` で
    使う。ここに集めておくことで、画面ごとに渡し忘れが起きない。

    バッジを渡さなければ「共有へ未保存」を自分で数えます ── 画面ごとに
    渡してもらう作りだと、**どこか1つで渡し忘れた画面だけ数が消えます**。
    """
    config = current_app.config
    return {
        "active": active,
        "nav": nav_items(save_badges() if badges is None else badges),
        "display_name": config["DISPLAY_NAME"],
        # 帯に常時出す版。**どれが入っている端末か**を聞かれたときに、
        # 画面を見れば答えられるようにする(出どころは config/app.json)
        "version_label": app_config.version_label(),
        # **中身から作った印**(`app.static_stamp`)。版だけだと「上げ忘れ」
        # で同じ値のまま入れ替わるので、どの build が動いているかは
        # こちらでしか分からない。画面(JS)が古いかどうかの判定にも使う
        "static_stamp": config.get("STATIC_STAMP", config["VERSION"]),
        "favicon": favicon(),
        "token": config["TOKEN"],
        "app_id": config["APP_ID"],
        # いま何が決まっているか。画面をまたいで持つ
        "ribbon": ribbon or empty_ribbon(),
        "health_poll_ms": app_config.health_poll_seconds() * 1000,
        # 心拍の間隔。**間隔の出どころは `idle_exit` ただ1つ** ── 画面と
        # サーバで別々に持つと、画面が送るより先にサーバが見切ってしまう
        "alive_poll_ms": idle_exit.HEARTBEAT_MS,
    }


def empty_ribbon() -> dict[str, Any]:
    """帯の既定値。値が無いことを「—」で示す(空欄にすると読めない)。"""
    return {"report_date": "—", "line": "—", "shift": "—", "page": "—",
            "chips": []}
