"""取り込み元 (共有の sqlite3) を探す・開いてみる

探索と診断の**業務側の入口**。ファイルの開き方そのもの(SMB 対策・
読み取り専用・リトライ)は ``dbkit.source_db`` が持ち、ここは
「保存用DB はどれか」「マスタDB はどれか」だけを決める。

【フォルダで持ち、ファイルは中から探す】
設定に持たせるのは**フォルダ**で、ファイル名は決め打ちにしない。
フルパスを設定させると、上流がファイル名を変えただけで動かなくなり、
現場からは「急に読めなくなった」としか見えない。
既定の名前で見つからなければ、そのフォルダにある sqlite3 を拾う。

【開けたかどうかは、引けたかどうかで決める】
``Path.exists()`` はファイルの中身を見ない。存在するのに壊れている・
WAL で作られていて共有では開けない、という場合に「見つかった」と
答えてしまい、最初の取り込みで初めて落ちる。``probe()`` は実際に
1文引いてから答える。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config
from .dbkit import source_db
from .dbkit.source_db import Probe, SourceConnection, SourceError, SourceUnreadable
from .logging_utils import get_logger

log = get_logger("sources")

#: 保存用DB(休み管理・削除履歴)の既定のファイル名
DATA_DB_NAME = config.SOURCE_FILE_DATA
#: マスタDB(班員名簿)の既定のファイル名
MASTER_DB_NAME = config.SOURCE_FILE_MASTER

__all__ = [
    "Probe", "SourceConnection", "SourceError",
    "SourceUnreadable", "find_data_db", "find_master_db", "list_source_files", "locate",
    "probe", "open_data_db", "open_master_db", "describe_sources",
]


# ---------------------------------------------------------------------------
# 探す
# ---------------------------------------------------------------------------
def list_source_files(directory: Optional[Path]) -> list[Path]:
    """そのフォルダにある取り込み元の候補。名前順。"""
    return source_db.list_source_files(directory)


def _configured(folder: Path) -> bool:
    """参照パスが決まっているか(``config`` は未設定のとき ``Path()`` を返す)。"""
    return bool(str(folder)) and folder != Path()


def _find_in(folder: Path, name: str) -> Optional[Path]:
    """決まっているフォルダなら、**読めないときは** ``SourceUnreadable``(無いとは言わない)。"""
    return source_db.find(folder, name, checked=_configured(folder))


def _listed(folder: Path) -> list[Path]:
    if _configured(folder):
        return source_db.list_source_files_checked(folder)
    return list_source_files(folder)


def _settings_readable() -> None:
    """参照パスはこの端末の設定にある。**読めないのを「未設定」と取り違えない**
    (取り違えると、旧設定・既定の場所を探して「見つかりません」と言う・別の所を使う)。"""
    from . import settings

    try:
        settings.load_checked()
    except settings.SettingsUnreadable as exc:
        raise SourceUnreadable(
            "この端末の設定ファイルを読めないため、取り込み元の置き場所が分かりません。"
            f"少しして自動でやり直します。({exc})") from exc


def find_data_db(directory: Optional[Path] = None) -> Optional[Path]:
    """保存用DB(連絡帳)を探す。

    順に見る:

    1. 参照パスのフォルダにある ``連絡帳``(拡張子違いも可)
    2. そのフォルダにある sqlite3 のうち、マスタ以外で最初のもの
    3. **旧設定のフルパス** ── フォルダ方式へ移る前に設定した端末が、
       更新の日に置き場所を見失わないようにするための逃げ道

    **次の候補へ進むのは、前の候補が本当に無いと確かめたときだけ。** 参照パスの
    フォルダに届かない・一覧を読めない・旧設定のファイルが見えない(権限・ロック)は
    ``SourceUnreadable``(=``SourceError``)。前は届かないフォルダを空のフォルダとして
    扱い、旧設定のフルパス(古い共有・手元の写し)へ移って、そこへ送っていた。
    """
    from . import settings

    if directory is None:
        _settings_readable()
    folder = Path(directory) if directory is not None else config.data_db_dir()
    named = _find_in(folder, DATA_DB_NAME)
    if named is not None:
        return named

    master_stem = Path(MASTER_DB_NAME).stem.casefold()
    for path in _listed(folder):
        if path.stem.casefold() == master_stem:
            continue
        log.info("保存用DBとして %s を使います", path.name)
        return path

    if directory is None:
        legacy = settings.access_data_path()
        if legacy and source_db.file_checked(Path(legacy)) is not None:
            log.info("旧設定のフルパスを使います: %s", legacy)
            return Path(legacy)
    return None


def find_master_db(directory: Optional[Path] = None) -> Optional[Path]:
    """マスタDB(班員名簿が入っているほう)を探す。

    **名前が一致したときだけ返す。** 保存用DBと同じフォルダに並んで
    いることが多く、緩い一致だと保存用のほうを誤って拾いかねない。

    マスタのフォルダが未設定なら、保存用と同じフォルダを見る ──
    現場の普通の置き方(1つの共有に両方置く)に合わせる。

    **マスタのフォルダに届かないときは、保存用のフォルダへ移らない**
    (``SourceUnreadable``)。前は移っていたので、保存用のフォルダに古いマスタが
    残っていると、共有が一瞬見えないだけで古い名簿・アクセス権限で入れ替えていた。
    """
    if directory is not None:
        return _find_in(Path(directory), MASTER_DB_NAME)

    _settings_readable()
    found = _find_in(config.master_db_dir(), MASTER_DB_NAME)
    if found is not None:
        return found
    return _find_in(config.data_db_dir(), MASTER_DB_NAME)


def locate(finder) -> tuple[Optional[Path], str]:
    """``find_data_db`` / ``find_master_db`` を呼んで ``(見つけたもの, 読めなかった理由)``。

    画面に出すだけの所が使う(読めないのを「見つかりません」と出さないため)。
    """
    try:
        return finder(), ""
    except SourceError as exc:
        log.warning("%s", exc)
        return None, str(exc)


# ---------------------------------------------------------------------------
# 開く
# ---------------------------------------------------------------------------
def probe(path: Optional[Path]) -> Probe:
    """実際に開いてみる。**駄目な理由と、次にすることまで返す。**"""
    return source_db.probe(path)


def open_data_db(*, read_only: bool = True) -> SourceConnection:
    """保存用DB を開く。見つからなければ ``SourceError``。"""
    found = find_data_db()
    if found is None:
        raise SourceError(
            f"保存用DB({DATA_DB_NAME})が見つかりません。"
            "設定画面の「参照パス」でフォルダを指定してください。")
    return source_db.connect(found, read_only=read_only)


def open_master_db(*, read_only: bool = True) -> SourceConnection:
    """マスタDB を開く。見つからなければ ``SourceError``。"""
    found = find_master_db()
    if found is None:
        raise SourceError(
            f"マスタDB({MASTER_DB_NAME})が見つかりません。"
            "設定画面の「参照パス」でフォルダを指定してください。")
    return source_db.connect(found, read_only=read_only)


def describe_sources() -> str:
    """取り込み元を1つずつ開いてみて、**結果を1枚にする**。

    起動のたびにログへ残す(基盤仕様書 2.6)。「読めません」と言われた
    ときに、これがあればログを見るだけで原因が分かる ── 無ければ、
    こちらが端末に行って同じことをやり直すことになる。
    """
    lines = []
    for label, finder in (("保存用DB", find_data_db),
                          ("マスタDB", find_master_db)):
        found, unreadable = locate(finder)
        result = probe(found)
        if unreadable:
            result.error = unreadable
        where = str(found) if found else ("(読めません)" if unreadable else "(見つかりません)")
        lines.append(f"{label}: {where} — {result.describe()}")
        hint = result.hint()
        if hint:
            lines.append(f"    → {hint}")
    return "\n".join(lines)
