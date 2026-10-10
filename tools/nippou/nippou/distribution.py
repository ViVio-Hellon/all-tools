"""配布設定 ── 1台で決めた設定を、配った先の端末でそのまま使う

    一度起動して配布先設定をする
    配布先フォルダが作られ配下に必要データセット
    起動時に配布先フォルダがある場合は配布先フォルダ内部を読み込む
    既存データがある場合はそちらが優先とする
    配布先フォルダは起動batと同じ階層に作ってください
    ViVio-Hellon/python-web-tools に配布設定の項目があるのでしっかりまねてください

【なぜ要るのか】
設定(共有の日報データ・参照用マスタ・出力先・音・管理者パスワード)は
端末ごとの `%LOCALAPPDATA%\\NippouTool\\runtime\\user_config.json` に入ります。
**ツールのフォルダをコピーしても付いて行きません。** 配った先で1台ずつ
打ち直すのは手間で、打ち間違えると**別のファイルを読み書きする**端末が
できます ── 共有の日報データの既定は「その端末の中」なので、
共有へ保存がその端末の中へ書かれ、共有には何も届きません。

【流れ】(python-web-tools の `packaging_tool/distribution.py` と同じ)
    1. 1台で起動して設定し、設定画面の「配布設定」で書き出す
    2. **起動用の Start.vbs / start.bat と同じフォルダ**に `配布設定\\` が
       でき、配るものが全部そこに入る
    3. フォルダごと配る(`scripts\\make_dist.bat` を使うと手元の物が紛れない)
    4. 配った先は起動したとき `配布設定\\` を見つけて読み込む

【`配布設定\\` の中身】**配布先に関わるものはここだけ**に置きます。

    配布設定\\
      設定.json          置き場所・出力先・音の名前・管理者パスワード(撹拌した値)・
                         この端末のライン(選んだときだけ)
      音\\*.wav           音声ファイル(選んだときだけ)
      はじめに読む.txt   何が入っているか・配った先で何が起きるか

【読み込むときの決まり】**その端末にすでにあるものは読み込みません。**

    設定       … その端末で値が入っている項目はそのまま。無い項目だけ埋める
                 (空の値は「無い」と数えます ── 下の【空の値】)
    音声ファイル … その端末で音の置き場所を決めてあればそのまま。無ければ
                 端末の中(`%LOCALAPPDATA%\\NippouTool\\data\\音声`)へ写して使う

音声ファイルを**端末の中へ写す**のは、あとで `配布設定\\` を片付けても
鳴るようにするためです(v3.73 はツールのフォルダの中を指していたので、
フォルダを消すと鳴らなくなりました)。

起動のたびに見に行きますが、埋まった項目は次から「すでにある」ので、
端末で直した値が戻されることはありません。狙って揃えたいときは、
設定画面の「配布設定を読み込み直す」(管理者パスワード)で上書きします。

【空の値】python-web-tools は「鍵があれば既存」と数えますが、こちらは
**空の値も「無い」と数えます。** この端末の設定ファイルには「既定に戻す」で
空の値が入ることがあり、それを既存と数えると、共有の日報データの置き場所が
**その端末の中のまま**残ります(配ったのに共有へ届かない)。

【パスワード】書き出す・消す・読み込み直すには管理者パスワードが要ります
(面の鍵を開けてあれば、もう一度は聞きません)。起動時の読み込みには
要りません ── `配布設定\\` を置いたのは、フォルダを配った管理者本人だからです。

【前の版の置き場所】v3.73 の `配布先\\`、v3.71〜3.72 の `config\\site.json` は、
`配布設定\\` が無いときだけ読みます(書き出すと `配布設定\\` が勝ちます)。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import app_config, config
from .logging_setup import get_logger
from .logic.sound import SOUND_KEYS

log = get_logger("distribution")

#: `配布設定\\` の名前。**起動用の Start.vbs / start.bat と同じフォルダ**に置く
DIR_NAME = "配布設定"
SETTINGS_NAME = "設定.json"
SOUNDS_DIRNAME = "音"
README_NAME = "はじめに読む.txt"
#: 起動用のファイル。`配布設定\\` はこれと同じ階層に作る
LAUNCHERS: tuple[str, ...] = ("Start.vbs", "start.bat")

FORMAT = 1

#: この端末が最後に読み込んだとき(設定画面に出すだけ)
KEY_APPLIED = "distribution_applied"

#: 前の版の置き場所(`配布設定\\` が無いときだけ読む)
LEGACY_DIR_NAME = "配布先"          # v3.73
LEGACY_FILE = ("config", "site.json")  # v3.71〜3.72

REFUSE_NEED_PASSWORD = "need_password"
REFUSE_BAD_INPUT = "bad_input"
REFUSE_FAILED = "failed"

#: 音声ファイルの名前の鍵(`sound_file_<音>`)。**音の並びは `logic/sound` から**
SOUND_NAME_KEYS: tuple[str, ...] = tuple(config.sound_file_key(key)
                                         for key in SOUND_KEYS)


@dataclass(frozen=True)
class Item:
    """配布設定に入れられる項目1つ。

    `keys` は `user_config.json` の鍵そのもの。**1つの項目で鍵が複数**の
    ものがあります(音の名前は出来事ごと・ラインと丸徳の設備番号)。
    """

    key: str
    label: str
    default: bool
    keys: tuple[str, ...] = ()

    @property
    def setting_keys(self) -> tuple[str, ...]:
        return self.keys or (self.key,)


ITEM_SOUND_NAMES = "sound_names"
ITEM_LINE = "terminal_line"

#: 入れられるもの。**並びは参照設定の面と同じ**(読みに行く先 → 書き出す先)。
#: **この端末のラインだけは既定で外す**(端末ごとに違う ── 入れたまま配ると
#: 全端末が同じラインになります)
ITEMS: tuple[Item, ...] = (
    Item(config.KEY_REFERENCE_DIR, "参照用マスタの参照パス", True),
    Item(config.KEY_WIP_DIR, "仕掛ロット・引当・受注の置き場所", True),
    Item(config.KEY_WIP_DIR2, "仕掛ロット・引当・受注の置き場所(2つ目)", True),
    Item(config.KEY_MATERIAL_DIR, "梱包資材マスタの置き場所", True),
    Item(config.KEY_TRANSMISSION_DIR, "伝送用ファイルの置き場所", True),
    Item(config.KEY_LINE_TARGET_FILE, "ライン毎目標の置き場所", True),
    Item(config.KEY_STOP_REASON_FILE, "停止内訳の置き場所", True),
    Item(config.KEY_VC_MASTER_DIR, "VC計算マスタの置き場所", True),
    Item(config.KEY_ACCESS_DIR, "共有の日報管理のパス", True),
    Item(config.KEY_ACCESS_DB_FILE, "日報データのファイル名", True),
    Item(config.KEY_MONTHLY_DIR, "月別書き出しの出力パス", True),
    Item(config.KEY_REPORT_OUT_DIR, "集計CSV・印刷用HTMLの出力パス", True),
    Item(config.KEY_REPORT_OUT_DIR2, "集計CSVの出力パス(2つ目)", True),
    Item(config.KEY_STANDARD_TIME_OUT_DIR, "標準作業時間CSVの出力パス", True),
    # 共有のフォルダを配れば、**どの端末のエラーも1か所で見られます**
    Item(config.KEY_LOG_DIR, "ログの出力パス", True),
    # 控え(LocalBackup)は**全端末で同じ所**でないと、管理者が別のPCから直せない
    Item(config.KEY_BACKUP_DIR, "日報入力データの控えの置き場所", True),
    Item(config.KEY_SOUND_DIR, "音声ファイルの参照パス", True),
    Item(ITEM_SOUND_NAMES, "音声ファイルの名前", True, SOUND_NAME_KEYS),
    Item(config.KEY_ADMIN_PASSWORD, "管理者パスワード", True),
    Item(ITEM_LINE, "この端末のライン(端末ごとに違う)", False,
         (config.KEY_TERMINAL_LINE, config.KEY_TERMINAL_MARU)),
)
ITEM_KEYS = frozenset(item.key for item in ITEMS)
ITEM_BY_KEY = {item.key: item for item in ITEMS}
#: 設定.json に書いてよい鍵(知らない鍵は読まない)
SETTING_KEYS = frozenset(k for item in ITEMS for k in item.setting_keys)

#: 一緒に配るファイル: (鍵, 画面の名前)。python-web-tools の「配置図」に当たる
FILE_SOUNDS = "sounds"
FILES: tuple[tuple[str, str], ...] = (
    (FILE_SOUNDS, "音声ファイル(この端末で鳴らしているもの)"),
)
FILE_KEYS = frozenset(key for key, _ in FILES)
FILE_LABELS = dict(FILES)


# ------------------------------------------------------------------
# 置き場所
# ------------------------------------------------------------------
def launch_dir() -> Path:
    """起動用の Start.vbs / start.bat があるフォルダ(ツールのフォルダ)。"""
    return app_config.APP_ROOT


def folder() -> Path:
    """`配布設定\\`。**起動用のファイルと同じ階層。**

    検証用に `NIPPOU_DIST_DIR` で差し替えられます(テストが本物の
    `配布設定\\` を読み書きしないように)。
    """
    override = os.environ.get("NIPPOU_DIST_DIR", "").strip()
    if override:
        return Path(override)
    return launch_dir() / DIR_NAME


def settings_path(base: Optional[Path] = None) -> Path:
    return (base or folder()) / SETTINGS_NAME


def sounds_dir(base: Optional[Path] = None) -> Path:
    return (base or folder()) / SOUNDS_DIRNAME


def _legacy_dirs() -> list[Path]:
    """前の版の置き場所。差し替えているとき(検証)は見ない。"""
    if os.environ.get("NIPPOU_DIST_DIR", "").strip():
        return []
    return [launch_dir() / LEGACY_DIR_NAME]


def _legacy_files() -> list[Path]:
    if os.environ.get("NIPPOU_DIST_DIR", "").strip():
        return []
    return [launch_dir().joinpath(*LEGACY_FILE)]


def integrated_where(folder) -> str:
    """日報複合ツールの一式の中(`<一式>\\tools\\<ツール>\\配布設定`)なら、置き場所と配り方の一言。違えば空。

    日報複合ツールでは、各ツールの配布設定は**そのツールのフォルダの中**にできます(一式の直下ではない)。
    単品のころの「起動用のファイルと同じフォルダ」と書くと、一式の直下を探して見つからない。
    """
    from pathlib import Path as _Path

    folder = _Path(folder)
    tool_dir = folder.parent
    root = tool_dir.parent.parent
    if tool_dir.parent.name != "tools" or not (root / "config" / "tools.json").is_file():
        return ""
    return (f"日報複合ツールの一式の中の「tools\\{tool_dir.name}\\{folder.name}」に入っています({folder})。"
            "配るときは一式の scripts\\make_dist.bat を実行してください"
            "(大設定と各ツールの配布設定がまとめて入ります。大設定の「配布設定」に一覧があります)。")


def terminal_sound_dir() -> Path:
    """配られた音声ファイルを写す、**この端末の中**の置き場所。"""
    return app_config.local_dir("data") / "音声"


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
@dataclass
class Bundle:
    """置いてある配布設定の中身。"""

    settings: dict[str, Any]
    sounds: list[Path] = field(default_factory=list)
    created_at: str = ""
    created_on: str = ""
    #: 読んだ所(`配布設定\\` か、前の版の置き場所)
    source: Optional[Path] = None
    #: 前の版の置き場所から読んだ
    legacy: bool = False


def _is_set(value: Any) -> bool:
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _read_json(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    """(中身, 読めなかった理由)。無ければ (None, "")。**例外にしない。**"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        return None, f"配布設定を読めません({path}): {exc}"
    if not isinstance(data, dict):
        return None, f"配布設定の形が違います({path}): 中身が {{ }} で囲まれていません"
    return data, ""


def _clean(raw: Any) -> dict[str, Any]:
    """知らない鍵と空の値を捨てる。"""
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in SETTING_KEYS and _is_set(v)}


def _sound_files(base: Path) -> list[Path]:
    try:
        return sorted(p for p in sounds_dir(base).iterdir() if p.is_file())
    except OSError:
        return []


def _read() -> tuple[Optional[Bundle], str]:
    """置いてある配布設定と、読めなかった理由。"""
    base = folder()
    if base.is_dir():
        data, error = _read_json(settings_path(base))
        if error:
            return None, error
        meta: dict[str, Any] = {}
        settings: dict[str, Any] = {}
        if data is not None:
            if data.get("format") != FORMAT:
                return None, (f"配布設定の形が違うため読みません({settings_path(base)}): "
                              f"format={data.get('format')!r}")
            settings = _clean(data.get("settings"))
            meta = data
        sounds = _sound_files(base)
        if not settings and not sounds:
            return None, ""
        return Bundle(settings=settings, sounds=sounds,
                      created_at=str(meta.get("created_at", "")),
                      created_on=str(meta.get("created_on", "")),
                      source=base), ""
    # 前の版(平らな { 鍵: 値 } の形)
    for old in _legacy_dirs():
        data, error = _read_json(old / SETTINGS_NAME)
        if error:
            return None, error
        if data is not None:
            return Bundle(settings=_clean(data), source=old, legacy=True), ""
    for old in _legacy_files():
        data, error = _read_json(old)
        if error:
            return None, error
        if data is not None:
            return Bundle(settings=_clean(data), source=old, legacy=True), ""
    return None, ""


def read() -> Optional[Bundle]:
    """置いてある配布設定。無い・読めない・形が違うなら None。"""
    bundle, error = _read()
    if error:
        log.warning("%s(無いものとして続行)", error)
    return bundle


def read_error() -> str:
    """置いてあるのに読めない理由。無い・読めるなら空。

    **壊れていても起動は止めません**(無いものとして動く)。代わりに
    設定画面とログでこれを言います ── 黙って既定で動くと、配ったはずの
    設定が効いていないことに誰も気づきません。
    """
    return _read()[1]


def value(key: str) -> Optional[Any]:
    """配布設定にあるその鍵の値。無ければ None。

    `user_settings.get` が、この端末で空の項目に使います(起動時の読み込みを
    待たずに同じ値で動くように ── 書き込めなかったときも)。

    音声ファイルを配っていて音の置き場所が入っていなければ、`配布設定\\音`
    を返します(端末の中へ写す前でも鳴るように)。
    """
    bundle = read()
    if bundle is None:
        return None
    found = bundle.settings.get(key)
    if _is_set(found):
        return found
    if key == config.KEY_SOUND_DIR and bundle.sounds:
        return str(sounds_dir(bundle.source))
    return None


# ------------------------------------------------------------------
# 画面に出す
# ------------------------------------------------------------------
def _item_of(key: str) -> Optional[Item]:
    for item in ITEMS:
        if key in item.setting_keys:
            return item
    return None


def _show(key: str, value: Any) -> str:
    if key == config.KEY_ADMIN_PASSWORD:
        return "(設定済み)"
    if isinstance(value, bool):
        return "する" if value else "しない"
    text = str(value)
    return text if text else "(既定)"


def contents(bundle: Bundle) -> list[dict[str, str]]:
    """中身を項目ごとに。**パスワードの値は出さない。**"""
    out: list[dict[str, str]] = []
    for item in ITEMS:
        values = [(k, bundle.settings[k]) for k in item.setting_keys
                  if k in bundle.settings]
        if not values:
            continue
        if item.key == ITEM_SOUND_NAMES:
            shown = "・".join(str(v) for _k, v in values)
        elif item.key == ITEM_LINE:
            shown = " ".join(str(v) for _k, v in values)
        else:
            shown = _show(item.key, values[0][1])
        out.append({"label": item.label, "value": shown})
    if bundle.sounds:
        out.append({"label": "音声ファイル",
                    "value": f"{SOUNDS_DIRNAME}\\ に{len(bundle.sounds)}件("
                             + "・".join(p.name for p in bundle.sounds) + ")"})
    return out


def applied_at() -> str:
    from . import user_settings

    found = user_settings.load_all().get(KEY_APPLIED)
    return str(found.get("at", "")) if isinstance(found, dict) else ""


def summary() -> dict[str, Any]:
    """設定画面に出す、配布設定のいま。**パスワードの値は出さない。**"""
    bundle, error = _read()
    out: dict[str, Any] = {
        "exists": bundle is not None,
        "path": str(folder()),
        # 日報複合ツールの一式の中なら、置き場所と配り方(make_dist.bat)の一言
        "where": integrated_where(folder()),
        "launch_dir": str(launch_dir()),
        "launchers": list(LAUNCHERS),
        "items": [{"key": i.key, "label": i.label, "default": i.default}
                  for i in ITEMS],
        "files": [{"key": k, "label": label} for k, label in FILES],
        "applied_at": applied_at(),
        "contents": [],
        "created_at": "",
        "created_on": "",
        "legacy": False,
        "source": "",
        "error": error,
        "reading": {"applied": [], "kept": []},
        "settings": {},
    }
    if bundle is None:
        return out
    out.update(contents=contents(bundle), created_at=bundle.created_at,
               created_on=bundle.created_on, legacy=bundle.legacy,
               source=str(bundle.source or ""),
               reading=reading(bundle),
               # 困りごとの札を出すため(パスワードは撹拌した値なので出しても
               # 平文にはならないが、画面には要らないので外す)
               settings={k: v for k, v in bundle.settings.items()
                         if k != config.KEY_ADMIN_PASSWORD})
    out["has_password"] = config.KEY_ADMIN_PASSWORD in bundle.settings
    return out


def reading(bundle: Bundle) -> dict[str, list[str]]:
    """**この端末が**配布設定をどう使っているか。

        applied … 配布設定と同じ値で動いている項目
        kept    … 配布設定にもあるが、この端末にすでにあった値が勝っている項目
    """
    from . import user_settings

    stored = user_settings.load_all()
    applied: list[str] = []
    kept: list[str] = []
    for item in ITEMS:
        keys = [k for k in item.setting_keys if k in bundle.settings]
        if not keys:
            continue
        mine = [stored.get(k) for k in keys]
        if all(not _is_set(v) or v == bundle.settings[k] for k, v in zip(keys, mine)):
            applied.append(item.label)
        else:
            kept.append(item.label)
    if bundle.sounds:
        (applied if _sounds_in_use(bundle) else kept).append("音声ファイル")
    return {"applied": applied, "kept": kept}


def _sounds_in_use(bundle: Bundle) -> bool:
    from . import user_settings

    mine = user_settings.load_all().get(config.KEY_SOUND_DIR)
    return (not _is_set(mine)) or str(mine) == str(terminal_sound_dir())


# ------------------------------------------------------------------
# 書き出す(配る側)
# ------------------------------------------------------------------
@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""
    applied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)   # すでにあったので読まなかったもの
    error: str = ""

    def as_text(self) -> str:
        """ログの1行。"""
        if self.error:
            return self.error
        if not self.applied and not self.kept:
            return "配布設定から読み込むものはありませんでした"
        parts = []
        if self.applied:
            parts.append(f"読み込み {len(self.applied)}件({'・'.join(self.applied)})")
        if self.kept:
            parts.append(f"既存を優先 {len(self.kept)}件({'・'.join(self.kept)})")
        return "配布設定: " + " / ".join(parts)


def _authorized(password: str, admin: bool) -> bool:
    from . import admin_password

    return admin or admin_password.verify(str(password or ""))


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def export(password: str, items: list[str], files: list[str], *,
           admin: bool = False) -> Result:
    """**この端末のいまの設定**を `配布設定\\` に書き出す(前の中身は置き換える)。

    この端末で一度も変えていない項目は入れない ── 配った先も同じ既定で
    動くので要らない(空で上書きしないためにも入れない)。

    音声ファイルを入れるときは、この端末で鳴らしている音(名前もこの端末の
    もの)を `音\\` へ写し、**音の置き場所の項目は入れません**(配った先は
    写した音を使う ── 共有の音の置き場所と2つあると、どちらで鳴るのか
    分からなくなるので)。
    """
    if not _authorized(password, admin):
        return Result(False, "配布設定を書き出すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    unknown = [k for k in list(items) + list(files)
               if k not in ITEM_KEYS and k not in FILE_KEYS]
    if unknown:
        return Result(False, f"知らない項目です: {', '.join(unknown)}",
                      REFUSE_BAD_INPUT)
    if not items and not files:
        return Result(False, "入れる項目を1つ以上選んでください。", REFUSE_BAD_INPUT)

    from . import user_settings

    current = user_settings.load_all()
    with_sounds = FILE_SOUNDS in files
    chosen = [ITEM_BY_KEY[k] for k in items]
    if with_sounds and ITEM_BY_KEY[ITEM_SOUND_NAMES] not in chosen:
        chosen.append(ITEM_BY_KEY[ITEM_SOUND_NAMES])   # 写した音と名前を揃える
    settings: dict[str, Any] = {}
    defaults: list[str] = []
    for item in chosen:
        if with_sounds and item.key == config.KEY_SOUND_DIR:
            continue                                   # 写した音を使う
        found = {k: current[k] for k in item.setting_keys if _is_set(current.get(k))}
        if found:
            settings.update(found)
        elif item.key != ITEM_SOUND_NAMES:
            defaults.append(item.label)

    sound_sources: list[Path] = []
    missing: list[str] = []
    if with_sounds:
        sound_sources, missing = _terminal_sounds()
        if not sound_sources:
            return Result(False, "この端末の音声ファイルが見つからないので、"
                                 f"音を入れられません({config.SETTINGS.sound_dir})。",
                          REFUSE_BAD_INPUT)
    if not settings and not sound_sources:
        return Result(False, "書き出せる設定がありません(" + "・".join(defaults)
                      + " はこの端末で変えていないため既定のままです)。",
                      REFUSE_BAD_INPUT)

    # **作ってから入れ替える。** 途中で失敗して、半分だけ新しい配布設定を
    # 残さない(配った先がそれを読んでしまう)
    target = folder()
    staging = target.with_name(target.name + ".作成中")
    meta = {"format": FORMAT, "created_at": _now(),
            "created_on": platform.node(), "settings": settings}
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        settings_path(staging).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if sound_sources:
            sounds_dir(staging).mkdir()
            for src in sound_sources:
                shutil.copyfile(src, sounds_dir(staging) / src.name)
        (staging / README_NAME).write_text(
            _readme(meta, [p.name for p in sound_sources]), encoding="utf-8-sig")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return Result(False, f"{target} に書けませんでした: {exc}", REFUSE_FAILED)

    names = [i.label for i in chosen
             if any(k in settings for k in i.setting_keys)]
    if sound_sources:
        names.append(f"音声ファイル{len(sound_sources)}件")
    message = (f"配布設定を書き出しました({len(names)}項目)。"
               + (integrated_where(target)
                  or (f"起動用の Start.vbs と同じフォルダの「{target.name}」に入っています({target})。"
                      "配るときは scripts\\make_dist.bat で配布用フォルダを作ってください"
                      "(このフォルダも入ります)。")))
    if defaults:
        message += (" 既定のままなので入れていないもの(配った先も既定で動きます): "
                    + "・".join(defaults) + "。")
    if missing:
        message += (" この端末に無かった音: " + "・".join(missing)
                    + "(配った先では文言だけ出ます)。")
    log.info("配布設定を書き出しました: %s", ", ".join(names))
    return Result(True, message, applied=names)


def _terminal_sounds() -> tuple[list[Path], list[str]]:
    """この端末で鳴らしている音声ファイル(あるもの, 無い名前)。"""
    from .logic.sound import SOUNDS

    found: list[Path] = []
    missing: list[str] = []
    seen: set[str] = set()
    for spec in SOUNDS:
        path = config.SETTINGS.sound_path(spec.key)
        if path is None or path.name in seen:
            continue
        seen.add(path.name)
        if path.is_file():
            found.append(path)
        else:
            missing.append(path.name)
    return found, missing


def _readme(meta: dict[str, Any], sounds: list[str]) -> str:
    lines = [
        "日報管理ツール 配布設定",
        "",
        f"作成: {meta['created_at']}({meta['created_on']})",
        "",
        "このフォルダに入っているもの",
    ]
    bundle = Bundle(settings=meta["settings"])
    for row in contents(bundle):
        lines.append(f"  {row['label']}: {row['value']}")
    if sounds:
        lines.append(f"  音声ファイル: {SOUNDS_DIRNAME}\\ に{len(sounds)}件"
                     f"({'・'.join(sounds)})")
    lines += [
        "",
        "置き場所",
        "  起動用の Start.vbs / start.bat と同じフォルダに置いたままにしてください。",
        "",
        "配った先で起きること",
        "  起動したときにこのフォルダを読み込みます。",
        "  その端末にすでにある設定は、読み込みません(上書きしない)。",
        "  音声ファイルは、音の置き場所を決めていない端末だけ、その端末の中へ写して使います。",
        "  揃えたいときは、設定画面の「この端末と配布」→「配布設定を読み込み直す」。",
        "  この端末のラインは、入れていなければ配った先で1度だけ決めてください",
        "  (決めるまでは日報入力の画面に知らせが出ます)。",
        "",
        "消してよいか",
        "  読み込んだあとの端末は、このフォルダが無くても同じ設定で動きます。",
        "  あとから配る端末のために、配るもとのフォルダには残してください。",
        "",
    ]
    return "\n".join(lines)


def remove(password: str, *, admin: bool = False) -> Result:
    if not _authorized(password, admin):
        return Result(False, "配布設定を消すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    target = folder()
    try:
        if target.exists():
            shutil.rmtree(target)
    except OSError as exc:
        return Result(False, f"消せませんでした: {exc}", REFUSE_FAILED)
    log.info("配布設定を消しました")
    return Result(True, "配布設定を消しました。この端末の設定はそのままです。")


# ------------------------------------------------------------------
# 読み込む(配られた側)
# ------------------------------------------------------------------
def _mark_applied() -> None:
    from . import user_settings

    user_settings.save(KEY_APPLIED, {"at": _now()})


def _apply(bundle: Bundle, *, overwrite: bool) -> Result:
    from . import user_settings

    result = Result()
    current = user_settings.load_all()
    to_write: dict[str, Any] = {}
    for item in ITEMS:
        keys = [k for k in item.setting_keys if k in bundle.settings]
        if not keys:
            continue
        mine = {k: current.get(k) for k in keys}
        if not overwrite and any(_is_set(v) for v in mine.values()):
            if any(_is_set(v) and v != bundle.settings[k] for k, v in mine.items()):
                result.kept.append(item.label)      # すでにある(違う値)
            continue
        if all(mine[k] == bundle.settings[k] for k in keys):
            continue                                # もう同じ
        for k in keys:
            to_write[k] = bundle.settings[k]
        if item.key == ITEM_LINE and config.KEY_TERMINAL_MARU not in keys:
            to_write[config.KEY_TERMINAL_MARU] = ""   # 丸徳でなければ設備番号は無し
        result.applied.append(item.label)

    if bundle.sounds:
        mine = current.get(config.KEY_SOUND_DIR)
        own = str(terminal_sound_dir())
        if overwrite or not _is_set(mine):
            copied = _copy_sounds(bundle)
            if copied:
                to_write[config.KEY_SOUND_DIR] = own
                result.applied.append(f"音声ファイル{copied}件")
        elif str(mine) != own:
            result.kept.append("音声ファイル")      # 音の置き場所を決めてある

    if to_write and not user_settings.save_many(to_write):
        result.error = "配布設定を、この端末の設定ファイルに書けませんでした"
        result.applied = []
        return result
    if result.applied:
        _mark_applied()
    return result


def _copy_sounds(bundle: Bundle) -> int:
    """配られた音声ファイルを、この端末の中へ写す。写した数。"""
    dest = terminal_sound_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for src in bundle.sounds:
            shutil.copyfile(src, dest / src.name)
    except OSError as exc:
        log.warning("音声ファイルを端末へ写せませんでした: %s", exc)
        return 0
    return len(bundle.sounds)


def apply_on_start() -> Result:
    """起動時に呼ぶ。`配布設定\\` があれば、**その端末に無いものだけ**読む。"""
    bundle, error = _read()
    if error:
        return Result(True, "", error=error)
    if bundle is None:
        return Result(True, "")
    result = _apply(bundle, overwrite=False)
    if result.applied:
        log.info("配布設定を読み込みました: %s(すでにあったので読まなかったもの: %s)",
                 ", ".join(result.applied), ", ".join(result.kept) or "なし")
        result.message = "配布設定を読み込みました"
    return result


def reapply(password: str, *, admin: bool = False) -> Result:
    """設定画面から。**すでにあるものも上書きして**読み込み直す。

    照合するのは**この端末の**パスワードです(まだ読み込んでいなければ既定)。
    """
    if not _authorized(password, admin):
        return Result(False, "配布設定を読み込み直すには管理者パスワードが要ります。",
                      REFUSE_NEED_PASSWORD)
    bundle, error = _read()
    if error:
        return Result(False, error, REFUSE_BAD_INPUT)
    if bundle is None:
        return Result(False, "配布設定が置かれていません。", REFUSE_BAD_INPUT)
    result = _apply(bundle, overwrite=True)
    if result.error:
        return Result(False, result.error, REFUSE_FAILED)
    if result.applied:
        result.message = (f"配布設定を読み込みました({len(result.applied)}項目: "
                          + "・".join(result.applied) + ")")
    else:
        result.message = "この端末はもう配布設定のとおりです。"
    return result


def describe() -> str:
    """ログの1行。**起動のたびに、何を読んでいるかを残す。**"""
    bundle, error = _read()
    if error:
        return error
    if bundle is None:
        return f"配布設定はありません({folder()})"
    note = "(前の版の置き場所)" if bundle.legacy else ""
    return (f"配布設定を読みます: {bundle.source}{note} "
            f"(設定 {len(bundle.settings)}件・音 {len(bundle.sounds)}件)")
