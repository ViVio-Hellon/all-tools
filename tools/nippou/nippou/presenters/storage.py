"""ファイルの置き場所 ── **この端末だけに残すもの**と、**複数のPCで共有するもの**

    ローカルに保存してそのPCで引き継いで使用するものは設定部にそういう
    ファイルがあるということを明記しておいてください
    (複数PCで共有するものと該当PCで引き継ぐものは違いますよね)

違います。このツールが触るファイルは2種類です:

    この端末だけ … 利用者ごとのローカル領域(`%LOCALAPPDATA%\\NippouTool\\`)。
                    **そのPCで次に起動したときに引き継ぐ**もの。ほかのPCからは
                    見えません。アプリのフォルダを入れ替えても消えません
    共有する     … 参照設定のパスで指す先。全端末が同じものを読み書きする

いちばん大事なのは**手元の日報**(`nippou_local.sqlite3`)です ── 打った日報は
「共有へ保存」を押すまで**このPCの中にしかありません。** PCを替える・初期化
する前に、共有へ保存を済ませてください。

画面は「設定・管理者 → この端末と配布」の面(`settings.html`)。ここは中身を
組むだけで、**ファイルは開きません**(あるか・大きさ・更新日時を見るだけ)。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .. import app_config, config
from ..config import SETTINGS


@dataclass
class Place:
    """ファイル(またはフォルダ)1つぶん。"""

    label: str
    path: str
    #: 何が入っているか
    holds: str
    #: 消したら・PCを替えたらどうなるか(この端末だけのもの)/
    #: どの設定で決まるか(共有するもの)
    note: str = ""
    is_dir: bool = False
    exists: bool = False
    size: str = ""
    updated: str = ""
    #: **消すと戻らない**(手元の日報)。画面で目立たせる
    important: bool = False
    #: 共有のはずの置き場所が、いまはこの端末の中を指している
    local_now: bool = False


def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _look(place: Place, path: Path) -> Place:
    """あるか・大きさ・更新日時。**開かない**(共有に届かなくても止めない)。"""
    try:
        if path.is_dir():
            place.exists, place.is_dir = True, True
            count, total, newest = 0, 0, 0.0
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_file():
                        stat = entry.stat()
                        count += 1
                        total += stat.st_size
                        newest = max(newest, stat.st_mtime)
            place.size = f"{count}件 / {_size_text(total)}" if count else "空"
            if newest:
                place.updated = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
        elif path.is_file():
            stat = path.stat()
            place.exists = True
            place.size = _size_text(stat.st_size)
            place.updated = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        place.exists = False
    return place


def _inside(path: Path, roots: list[Path]) -> bool:
    """`path` がこの端末のローカル領域の中か。"""
    for root in roots:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _lock_path() -> Path:
    try:
        import launch_guard
        return launch_guard.lock_path()
    except Exception:                                 # noqa: BLE001 - 見せるだけ
        return app_config.local_dir("runtime") / "nippou.lock"


def local_places() -> list[Place]:
    """**この端末だけ**に残し、次に起動したときに引き継ぐもの。"""
    from ..vc import masters as vc_masters
    from .. import distribution, source_db

    items: list[tuple[str, Callable[[], Path], str, str, bool]] = [
        ("手元の日報", lambda: SETTINGS.sqlite_path,
         "打った日報(自動保存・保存(確定))・集計・共有へ未送信の印・共有保存の履歴",
         "保存のたびに控え(LocalBackup)へも同じページを書き、手元に無いページは起動の"
         "ときに控えから戻します。控えに届いていないあいだは、ここにしかありません"
         "(下の「控え」に、まだ写せていないページの数が出ます)。PCを替える・初期化する前に、"
         "控えに届いていることか、共有へ保存を済ませてください", True),
        ("この端末の設定", lambda: config.USER_CONFIG_PATH,
         "この端末のライン・参照設定のパス・音のファイル名・管理者パスワード(撹拌して)",
         "ほかのPCへ同じ設定を持っていくには、下の「配布設定」で書き出します。"
         "消すと既定(と配布設定)に戻ります", False),
        ("参照マスタの写し", source_db.copy_dir_path,
         "共有の参照マスタ(SIKALOT・伝送用ファイル など)を写したもの",
         "消しても、次に読むときに共有から写し直します", False),
        ("VC計算マスタの控え", vc_masters.cache_path,
         "最後に読めた VC計算マスタの中身",
         "マスタに届かないときだけ使います。消してもかまいません", False),
        (OUTPUT_LABEL, _default_output_dir,
         "参照設定で出力先を決めていないときの、集計CSVと印刷用HTML",
         "ほかのPCからは見えません。参照設定で共有のフォルダを指すと、みんなが開けます",
         False),
        ("配られた音声", distribution.terminal_sound_dir,
         "配布設定で配られた音声ファイルを写したもの",
         "音の置き場所を決めていない端末だけが使います", False),
        ("ログ", lambda: SETTINGS.default_log_dir,
         "動いた記録とエラーの記録(困ったときに見るもの)・起動の記録",
         "参照設定で「ログの出力パス」を決めると、動いた記録とエラーの記録は"
         "そちらへ書きます(起動の記録だけはいつもここ)。設定した先に書けない"
         "ときもここへ書きます。消してもかまいませんが、なぜなぜの手がかりが"
         "消えます", False),
        ("多重起動の印", _lock_path,
         "動いているあいだだけあるファイル(2つ目が立たないように)",
         "止まれば消えます。残っていても次の起動が片付けます", False),
    ]
    out = []
    for label, getter, holds, note, important in items:
        try:
            path = Path(getter())
        except Exception:                             # noqa: BLE001 - 画面は出す
            continue
        place = _look(Place(label=label, path=str(path), holds=holds,
                            note=note, important=important), path)
        if label == OUTPUT_LABEL and not _default_output():
            place.note = ("いまはここへは出していません ── 出力先は参照設定で "
                          f"{SETTINGS.report_output_dir} に決めてあります")
        out.append(place)
    return out


OUTPUT_LABEL = "集計CSV・印刷用HTML(既定の出力先)"


def _default_output() -> bool:
    """集計CSVの出力先を決めていない(= この端末の中へ出している)。"""
    from .. import user_settings
    stored = user_settings.load_all().get(config.KEY_REPORT_OUT_DIR)
    return not (isinstance(stored, str) and stored.strip())


def _default_output_dir() -> Path:
    """決めていないときの出力先(`config.SETTINGS.report_output_dir` の既定)。"""
    from ..config import _app_dir_override
    override = _app_dir_override()
    return override / "reports" if override else app_config.local_dir("work")


def shared_places() -> list[Place]:
    """**複数のPCで共有するもの**(参照設定のパスで指す先)。"""
    from .settings import PROTECTED_LABELS

    def label_of(key: str) -> str:
        return PROTECTED_LABELS.get(key, key)

    items: list[tuple[str, Callable[[], Optional[Path]], str, str]] = [
        ("日報データ(共有)", lambda: SETTINGS.access_db_path,
         "「共有へ保存」で送った日報・直ごとの集計・共有保存の履歴",
         label_of(config.KEY_ACCESS_DIR)),
        ("日報入力データの控え(LocalBackup)", lambda: SETTINGS.local_backup_root,
         "保存のたびに書く、各端末の日報(ライン名のフォルダごと)。Administrator が"
         "「記録を見る」で全ラインを見て直す", label_of(config.KEY_BACKUP_DIR)),
        ("標準作業時間", lambda: SETTINGS.standard_time_db_path,
         "送った直の実績(標準作業時間の画面が読む)", label_of(config.KEY_ACCESS_DIR)),
        ("仕掛ロット・引当・受注", lambda: SETTINGS.wip_master_dir,
         "SIKALOT / SIKAHIKI / SIKAODR", label_of(config.KEY_WIP_DIR)),
        ("仕掛ロット・引当・受注(2つ目)", lambda: SETTINGS.wip_master_dir_2,
         "1つ目で見つからないときに見る", label_of(config.KEY_WIP_DIR2)),
        ("梱包資材マスタ", lambda: SETTINGS.material_master_dir,
         "梱包資材マスタ / コイル割り数(LS4LOT)", label_of(config.KEY_MATERIAL_DIR)),
        ("伝送用ファイル", lambda: SETTINGS.transmission_master_dir,
         "停止理由内訳・直の境界時刻", label_of(config.KEY_TRANSMISSION_DIR)),
        ("VC計算マスタ", lambda: SETTINGS.vc_master_path,
         "VC長さ計算の品種・定尺・早見表", label_of(config.KEY_VC_MASTER_DIR)),
        ("ライン毎目標", lambda: SETTINGS.line_target_path,
         "45度線の目標枚数", label_of(config.KEY_LINE_TARGET_FILE)),
        ("停止内訳", lambda: SETTINGS.stop_reason_csv_path,
         "停止理由の一覧(あれば伝送用ファイルより先)", label_of(config.KEY_STOP_REASON_FILE)),
        ("月別の書き出し", lambda: SETTINGS.monthly_dir,
         "月替わりの1か月ぶん", label_of(config.KEY_MONTHLY_DIR)),
        ("集計CSV・印刷用HTML", lambda: SETTINGS.report_output_dir,
         "保存(確定)のたびに書く集計CSV", label_of(config.KEY_REPORT_OUT_DIR)),
        ("集計CSV(2つ目)", lambda: SETTINGS.report_output_dir_2,
         "共有へ保存のときに出す集計CSV", label_of(config.KEY_REPORT_OUT_DIR2)),
        ("音声ファイル", lambda: SETTINGS.sound_dir,
         "催促などで鳴らす音", label_of(config.KEY_SOUND_DIR)),
    ]
    roots = [app_config.local_root(), SETTINGS.app_dir]
    out = []
    for label, getter, holds, key_label in items:
        try:
            path = getter()
        except Exception:                             # noqa: BLE001 - 画面は出す
            continue
        if path is None:                              # 使っていない(2つ目の出力先)
            out.append(Place(label=label, path="(決めていません ── 出しません)",
                             holds=holds, note=key_label))
            continue
        place = _look(Place(label=label, path=str(path), holds=holds,
                            note=key_label), Path(path))
        place.local_now = _inside(Path(path), roots)
        out.append(place)
    return out


def storage_view() -> dict:
    return {
        "local_root": str(app_config.local_root()),
        "local": [asdict(p) for p in local_places()],
        "shared": [asdict(p) for p in shared_places()],
    }
