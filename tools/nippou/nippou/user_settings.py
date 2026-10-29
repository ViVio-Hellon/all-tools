"""この端末の設定を覚えておく (VBA `GetSetting`/`SaveSetting` の代わり)

VBA版は Windows のレジストリに置いていた。

    GetSetting("日報管理", "Config", "Line", "L1")
    SaveSetting "日報管理", "Config", "Line", "LVC"

Python版はレジストリを使わず、利用者ごとのローカル領域の JSON
(`config.USER_CONFIG_PATH`)に書く。読み書きのたびに開くので、外から
書き換えても次の読み取りで反映される ── レジストリと同じ感覚で使える。

【なぜ端末ごとなのか】
ラインごとに1台のPCで動かす前提なので、「このPCはどのラインか」
「参照するマスタはどこにあるか」は端末の持ちものになる。共有フォルダに
置くと、あるラインの設定が別のラインに効いてしまう(基盤仕様書 2.7)。

【「無い」と「読めない」を取り違えない】
設定ファイルが**無い**(はじめて動かす端末)のと、**あるのに読めない**
(ほかの処理が開いている・権限が無い)のとは別のことです。以前はどちらも
空の設定として扱っていたので、読めなかった瞬間に1項目を保存すると、
ほかの設定(置き場所・管理者パスワード・音など)が全部消えていました。
管理者パスワードも「変えていない」とみなされ、既定の値で通っていました
(別ツールで、共有を読めなかったのを「表が無い」と取り違えて送り直し、
重複が大量にできた事案を受けた点検)。

    画面に出す・決めるだけ … `load_all` / `get`(読めなければ空で続ける)
    書く前・照合する前   … `load_checked`(読めなければ `SettingsUnreadable`)
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .logging_setup import get_logger

log = get_logger("user_settings")

# ログに値を出さない鍵。伏せるだけで、保存はふつうに行う
HIDDEN_IN_LOG = ("admin_password",)

#: 読めないとき、読み直す回数と間(秒)。Windows では、ほかの処理が置き換えて
#: いる瞬間に開くと断られる(一瞬のことが多い)
READ_TRIES = 3
READ_WAIT_SEC = 0.1
#: 置き換えが断られたとき、やり直す回数と間(秒)。Windows では、ほかの処理が
#: ファイルを開いている瞬間に置き換えると「アクセスが拒否されました」(WinError 5)になる
REPLACE_TRIES = 20
REPLACE_WAIT_SEC = 0.05

#: 「読んで・直して・書く」を1つずつにする錠。2つの保存が重なると、どちらも
#: 同じ古い中身を読んで自分の分だけ直して書くので、後に書いたほうが先の変更を消す
LOCK = threading.RLock()

# 書けなかった理由(処理ごと。画面の知らせに添える ── `last_problem`)
_local = threading.local()


class SettingsUnreadable(OSError):
    """設定ファイルは**ある**のに読めない(ロック中・権限・一時的な不調)。

    **「無い・空」とは違う。** これが出たら書かない・照合で通さない。
    """


def load_checked() -> dict[str, Any]:
    """書く前・照合する前に読む。**無ければ空、あるのに読めなければ** `SettingsUnreadable`。

    読めないときは少し間をおいて読み直してから諦める(一瞬のことが多いので)。
    中身が JSON として壊れているときはこれまでどおり空とみなす ── 書くときは
    一時ファイルから置き換えるので書きかけの中身は残らない。壊れているなら
    本当に壊れていて、保存すれば直る。
    """
    path = config.USER_CONFIG_PATH
    for attempt in range(READ_TRIES):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if attempt + 1 >= READ_TRIES:
                raise SettingsUnreadable(
                    f"この端末の設定ファイル({path})を読めませんでした: {exc}") from exc
            time.sleep(READ_WAIT_SEC)
            continue
        except ValueError as exc:                 # 文字の符号が壊れている
            log.warning("設定ファイルの中身が読めない形でした(空として扱います): %s", exc)
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            log.warning("設定ファイルの中身が読めない形でした(空として扱います): %s", exc)
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def load_all() -> dict[str, Any]:
    """設定ファイル全体を読む(**画面に出す・決めるだけ**)。**例外にしない。**

    ここで落ちると設定画面すら開けなくなり、直す手立てが無くなる。
    読めなければ空として扱い、既定値で動かす。**書くときは使わない**
    (`save` / `save_many` / `forget` は `load_checked` で読み、読めなければ書かない)。
    """
    try:
        return load_checked()
    except SettingsUnreadable as exc:
        log.warning("%s(既定値で続行)", exc)
        return {}


def problem() -> str:
    """設定ファイルを読めない理由。読める(か、無い)なら空。"""
    try:
        load_checked()
    except SettingsUnreadable as exc:
        return str(exc)
    return ""


def last_problem() -> str:
    """この処理で最後に書けなかった理由(画面の知らせに添える)。書けていれば空。"""
    return getattr(_local, "problem", "")


def last_unreadable() -> bool:
    """この処理で最後に書けなかったのは、**いまの中身を読めなかったから**か。"""
    return bool(getattr(_local, "unreadable", False))


def _note(text: str, *, unreadable: bool = False) -> None:
    _local.problem = text
    _local.unreadable = unreadable


def _is_set(value: Any) -> bool:
    """値が入っているか。**空文字は「既定に戻した」印**なので入っていない。"""
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def get(key: str, default: Any = None) -> Any:
    """VBA `GetSetting` 相当。**端末の値 → 配布設定 → `default`。**

    端末で変えていなければ、起動用の Start.vbs と同じフォルダの
    `配布設定\\設定.json`(`distribution`)を見ます。起動時の読み込みで
    端末に写るので普段はそちらが効きますが、写す前(起動の途中)や写せ
    なかったときも同じ値で動くように。そこにも無ければこれまでどおり。
    """
    data = load_all()
    mine = data.get(key)
    if _is_set(mine):
        return mine
    from . import distribution

    shared = distribution.value(key)
    if shared is not None:
        return shared
    return data.get(key, default)


def terminal_value(key: str, data: dict[str, Any] | None = None) -> Any:
    """**この端末の**値(配布設定は見ない)。無ければ None。

    書いていなくても、前の版の鍵に値があればそれ(`config.TIME_SPLIT_INHERITS` ── 「時間を割り振る」の
    入り切りを2つに分けたとき、前の1つの入り切りを引き継ぐため)。配布設定を読み込むときも、この値を
    「この端末の値」として比べる(`distribution`)。
    """
    data = load_all() if data is None else data
    mine = data.get(key)
    if _is_set(mine):
        return mine
    older = config.TIME_SPLIT_INHERITS.get(key)
    if older and _is_set(data.get(older)):
        return data.get(older)
    return None


# 値の出どころ。設定画面が「配布設定の値」の札を付けるのに使う
ORIGIN_TERMINAL = "terminal"
ORIGIN_SITE = "site"


def origin(key: str) -> str:
    """その値はどこから来たか ── 端末 / 配布設定 / どちらでもない(空)。

    **配布設定から読み込んだ値は「配布設定」と言う。** 起動時の読み込み
    (`distribution.apply_on_start`)で端末の設定に写るので、そのままでは
    「端末で変えた値」と見分けが付きません。配布設定と同じ値なら
    配布設定から、と数えます。
    """
    from . import distribution

    mine = load_all().get(key)
    shared = distribution.value(key)
    if _is_set(mine):
        return ORIGIN_SITE if shared is not None and mine == shared else ORIGIN_TERMINAL
    return ORIGIN_SITE if shared is not None else ""


def save(key: str, value: Any) -> bool:
    """VBA `SaveSetting` 相当。書き込めなければ False。"""
    return save_many({key: value})


def save_many(values: dict[str, Any]) -> bool:
    """まとめて書く。**途中で落ちて半分だけ残る**のを避ける。

    設定画面は複数の項目を1回で送ってくるので、1つずつ書くと
    3つ目で失敗したときに1つ目と2つ目だけ変わった状態が残る。

    **いまの中身を読めなければ書かない**(False)。空に足して書くと、
    ほかの設定が全部消える。
    """
    with LOCK:
        data = _load_for_write()
        if data is None:
            return False
        data.update(values)
        return _write(data)


def forget(key: str) -> bool:
    """1つ消す(= 既定値に戻す)。いまの中身を読めなければ消さない(False)。"""
    with LOCK:
        data = _load_for_write()
        if data is None:
            return False
        if key not in data:
            _note("")
            return True
        del data[key]
        return _write(data)


def _load_for_write() -> dict[str, Any] | None:
    """書く前に読む。読めなければ None(理由はログと `last_problem` に残す)。"""
    try:
        return load_checked()
    except SettingsUnreadable as exc:
        log.warning("設定を書きませんでした(ほかの設定を消さないため): %s", exc)
        _note(f"{exc}。ほかの設定を消さないよう、書きませんでした", unreadable=True)
        return None


def _replace(tmp: Path, path: Path) -> None:
    """一時ファイルで置き換える。**Windows で一瞬断られたら、少し待ってやり直す。**"""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 >= REPLACE_TRIES:
                raise
            time.sleep(REPLACE_WAIT_SEC)


def _write(data: dict[str, Any]) -> bool:
    """全体を書く。**一時ファイルに書いてから置き換える**(途中で落ちても半分の中身を残さない)。

    以前は設定ファイルへ直に書いていたので、書いている途中で落ちると中身が
    半分になり、次に読んだとき「壊れている = 空」になって全部の設定が消えた。
    一時ファイルの名前は書くたびに変える(重なった保存が同じ一時ファイルに書かない)。
    """
    path = config.USER_CONFIG_PATH
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()                      # 一時ファイルは残さない
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("設定ファイルに書けませんでした: %s", exc)
        _note(f"この端末の設定ファイル({path})に書けませんでした: {exc}")
        return False
    _note("")
    return True


def log_saved(keys: list[str]) -> None:
    """何を保存したかを記録する。**値そのものは選んで出す。**"""
    shown = [k for k in keys if k not in HIDDEN_IN_LOG]
    hidden = [k for k in keys if k in HIDDEN_IN_LOG]
    if shown:
        log.info("設定を保存しました: %s", ", ".join(sorted(shown)))
    if hidden:
        log.info("設定を保存しました(値は伏せます): %s", ", ".join(sorted(hidden)))
