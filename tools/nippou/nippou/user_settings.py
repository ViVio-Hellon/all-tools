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
"""
from __future__ import annotations

import json
from typing import Any

from . import config
from .logging_setup import get_logger

log = get_logger("user_settings")

# ログに値を出さない鍵。伏せるだけで、保存はふつうに行う
HIDDEN_IN_LOG = ("admin_password",)


def load_all() -> dict[str, Any]:
    """設定ファイル全体を読む。**壊れていても例外にしない。**

    ここで落ちると設定画面すら開けなくなり、直す手立てが無くなる。
    読めなければ空として扱い、既定値で動かす。
    """
    path = config.USER_CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("設定ファイルを読めませんでした(既定値で続行): %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


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
    data = load_all()
    data[key] = value
    return _write(data)


def save_many(values: dict[str, Any]) -> bool:
    """まとめて書く。**途中で落ちて半分だけ残る**のを避ける。

    設定画面は複数の項目を1回で送ってくるので、1つずつ書くと
    3つ目で失敗したときに1つ目と2つ目だけ変わった状態が残る。
    """
    data = load_all()
    data.update(values)
    return _write(data)


def _write(data: dict[str, Any]) -> bool:
    try:
        config.USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.USER_CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("設定ファイルに書けませんでした: %s", exc)
        return False
    return True


def forget(key: str) -> bool:
    """1つ消す(= 既定値に戻す)。"""
    data = load_all()
    if key not in data:
        return True
    del data[key]
    return _write(data)


def log_saved(keys: list[str]) -> None:
    """何を保存したかを記録する。**値そのものは選んで出す。**"""
    shown = [k for k in keys if k not in HIDDEN_IN_LOG]
    hidden = [k for k in keys if k in HIDDEN_IN_LOG]
    if shown:
        log.info("設定を保存しました: %s", ", ".join(sorted(shown)))
    if hidden:
        log.info("設定を保存しました(値は伏せます): %s", ", ".join(sorted(hidden)))
