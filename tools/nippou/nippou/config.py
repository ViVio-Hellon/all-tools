"""Central configuration for paths, timing constants and the admin password.

Values here mirror constants that were scattered across the original VBA
project (module-level Public variables, hard-coded paths in ``shiftTime`` /
``Pass_Setting`` / ``NippouDB_GetDBPath`` etc.). They are collected in one
place so a real deployment only has to edit this file (or override with
environment variables) instead of hunting through source code.

【``app_config.py`` との違い】
ここは**業務の定数**(Accessの置き場所・テーブル名・直の判定・管理者
パスワード・タイマーの閾値)を持つ。アプリという入れ物の値(ID・ポート・
ローカル領域)は :mod:`nippou.app_config` の担当で、起動基盤はそちらしか
見ない(基盤仕様書 2.5「起動処理とアプリ本体の分離」)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import app_config


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def _app_dir_override() -> Path | None:
    """``NIPPOU_APP_DIR`` が指す実行時ファイルの置き場所(未設定なら None)。

    設定されていれば、SQLite・ログ・出力をまとめてこの1フォルダの下へ置く
    (テストが1つの一時フォルダへ隔離するのに使う)。
    """
    override = os.environ.get("NIPPOU_APP_DIR")
    if override and override.strip():
        return Path(override.strip())
    return None


def _default_app_dir() -> Path:
    """実行中に変化するもの(SQLite)の置き場所。

    既定は利用者ごとのローカル領域(``%LOCALAPPDATA%\\NippouTool\\data``)。
    アプリ本体は共有フォルダに置かれうるので、そちらへ書かない
    (基盤仕様書 2.7)。``NIPPOU_APP_DIR`` で従来どおり差し替えられる。
    """
    return _app_dir_override() or app_config.local_dir("data")


# ------------------------------------------------------------------
# この端末の設定ファイル(`user_settings`)
# ------------------------------------------------------------------
def _user_config_path() -> Path:
    override = _app_dir_override()
    root = override if override else app_config.local_dir("runtime")
    return root / "user_config.json"


# **属性として読めるようにしておく。** `config.USER_CONFIG_PATH` の形で
# 使いたいが、置き場所はテストが差し替えるので、読むたびに決める必要が
# ある。モジュールに `__getattr__` を置くとその両方が満たせる
def __getattr__(name: str):
    if name == "USER_CONFIG_PATH":
        return _user_config_path()
    raise AttributeError(name)


# ------------------------------------------------------------------
# 参照パス ── どこを見に行くか
# ------------------------------------------------------------------
# 【なぜ設定で変えられる必要があるのか】
# 参照用マスタは共有フォルダに1つ置いて全ラインが見る。ところが置き場所は
# 課の都合で動くし、共有に届かない端末では手元の写しを指したい。
# ソースに埋めると、そのたびに配り直すことになる。
#
# 優先順は **設定画面 → 環境変数 → 既定** の3段。設定画面の値は
# `user_settings` に入り、`resolve_dir()` を通してから使う。
KEY_ACCESS_DIR = "access_dir"            # 共有の日報データの置き場所
KEY_REFERENCE_DIR = "gw_reference_dir"   # 参照用マスタの置き場所
KEY_SOUND_DIR = "sound_dir"              # 音声ファイルの置き場所
KEY_MONTHLY_DIR = "monthly_dir"          # 月替わりで書き出す先(自動集計)
# ③反映の書き先のファイル名。**拡張子で形式が決まる**
#   日報データ.sqlite3 … sqlite3(標準ライブラリだけで書ける。無ければ作る)
#   日報データ.accdb   … Access(cscript.exe / ODBC ドライバが要る)
#
# **既定は sqlite3**(`access_db_filename` = "日報データ.sqlite3")。
# 上流の変換が Access から sqlite3 へ移り終わっているためです。
# `.accdb` しか置かれていないフォルダでは `_pick_source` がそちらを
# 拾うので、まだ移していない現場もそのまま動きます ── 勝手に
# 切り替えはしません。振り分けは `pusher.is_sqlite_target()` の1か所
KEY_ACCESS_DB_FILE = "access_db_filename"
KEY_ADMIN_PASSWORD = "admin_password"    # 撹拌済み(`admin_password.py`)
# ライン毎の目標枚数(45度線)を書いたCSV。**メモ帳で直せるものを正にする**
# ── マスタを直すには Access なりツールなりを開くことになり、「目標だけ
# 変えたい」には重すぎる。名前でも、フォルダ付きでも、フルパスでもよい:
#   ライン毎目標.csv          … 参照用マスタの置き場所の下
#   目標\\今期.csv             … アプリのフォルダからの相対
#   \\\\server\\共有\\目標.csv   … そのまま
KEY_LINE_TARGET_FILE = "line_target_file"
# 停止内訳のCSV。書き方はライン毎目標と同じ(名前でも、フォルダ付きでも、
# フルパスでもよい)。**あれば伝送用ファイルの表より先に読む**
# (分類ごと。`logic/stop_csv`)── 停止理由を足すのに管理者パスワードを
# 要らなくするため
KEY_STOP_REASON_FILE = "stop_reason_file"
# VC計算マスタの置き場所(フォルダ)。**空なら参照用マスタと同じフォルダ。**
# vc-calculator と1つのマスタを使うとき、あちらのフォルダを指せるように
# (`SETTINGS.vc_master_path` ── 名前の優先は `VC計算マスタ` → `vc_master`)
KEY_VC_MASTER_DIR = "vc_master_dir"
# 参照用マスタを**置き場所ごと3つに分ける**(v4.16.0)。どれも**空なら参照用マスタと
# 同じフォルダ**(これまでどおり)。
#
#     仕掛ロット・仕掛引当・仕掛受注 … 別の場所に置くので別に設定できるように
#     梱包資材マスタ・コイル割り数(LS4LOT)
#     伝送用ファイル
KEY_WIP_DIR = "wip_master_dir"                    # SIKALOT / SIKAHIKI / SIKAODR
KEY_MATERIAL_DIR = "material_master_dir"          # 梱包資材マスタ / LS4LOT
KEY_TRANSMISSION_DIR = "transmission_master_dir"  # 伝送用ファイル

# **この端末のライン**と丸徳の設備番号。据え付けのときに設定画面で決める。
#
# 以前は覚えていませんでした(プロセスの中だけ)。アプリは画面を閉じると
# 90秒で自分から終わる(`idle_exit`)ので、**起動し直すたびに L1 に
# 戻って**いました ── LVC の端末で打った日報が L1 のキーで保存され、
# 共有の `T_日報ヘッダー_L1` へ送られます。VBA はレジストリに持っていた
# (`SaveSetting "日報管理", "Config", "Line"`)ものです。
#
# **端末ごとの値**なので、配布設定(`distribution`)には既定で入れません
# (選べば入れられる ── python-web-tools の「拠点」と同じ)。
KEY_TERMINAL_LINE = "terminal_line"
KEY_TERMINAL_MARU = "terminal_maru_sub"

# マスタの「アクセス権限」の表から**前に当てたライン**(v4.12.0)。
#
# 表の値が**これから変わったときだけ**、この端末のラインを当て直します
# (`logic/access_rights.should_apply`)── 毎回当てると、管理者パスワードで
# 変えたラインが、起動し直すたびに表の値へ戻ってしまうので
KEY_ACCESS_LINE_APPLIED = "access_rights_line"

# CSV と印刷用HTMLの書き出し先。**どこに出たのか分からない**という
# 声があったので、設定できるようにした(既定はローカル領域の `work`)。
# 共有のフォルダを指せば、出したCSVを他の人がそのまま開ける
KEY_REPORT_OUT_DIR = "report_output_dir"

# 集計CSVの**2つ目の**出力先。空なら出さない(1つ目だけ)。
#
#     集計データの出力先を２つ設定したいです(使用、アクセス権の関係)
#
# 見る人とアクセス権で分けたいときのためです(例: 1つ目は現場の共有、
# 2つ目は管理の人だけが入れるフォルダ)。**中身は1つ目と同じ3本**を、
# 同じ `ライン名/集計/年月/日` の形で出します。印刷用HTMLと月別の
# 書き出しは1つ目(と月別の出力パス)だけです ── 集計のデータではないので
KEY_REPORT_OUT_DIR2 = "report_output_dir_2"

# 標準作業時間のCSV(標準の表・同条件の作業)の出力先。
#
#     標準作業時間： CSVに出す 出力先を指定できるようにしてください
#
# **空なら集計CSVと同じ先**(`KEY_REPORT_OUT_DIR`)。集計CSVは現場の共有へ
# 出すことが多い一方、標準作業時間は見る人(班長・管理)が違うので、
# 別のフォルダを指せるようにしてあります
KEY_STANDARD_TIME_OUT_DIR = "standard_time_output_dir"

# ログ(記録)の置き場所。
#
#     エラー等の後追いが現状できないと感じている
#     ログ出力は設定でパス指定できるようにする
#
# **空ならこの端末のローカル領域の `logs`**(前からの場所)。共有のフォルダを
# 指せば、どの端末で起きたエラーも1か所で見られます(ファイル名に端末の
# 名前が入るので混ざりません)。届かないときは手元の `logs` へ書きます
# (`services/event_log`)── 記録が書けないことで止めない。
#
# 起動の記録(launcher.log / guard.log)は**ここに従いません。** 起動できない
# ときに読むものなので、いつも手元に置きます(起動待ちの画面もそこを指す)
KEY_LOG_DIR = "log_dir"

# 日報入力データの**控え**の置き場所(v4.12.0)。この下の `LocalBackup\ライン名\`
# に、保存のたびに同じページを書きます(手元と両方)。
#
#     日報入力データがローカル保存とのことですが指定パスにも保存できるように
#     (ローカルと両方)ローカルになければパスを見る / フォルダ名は LocalBackup
#
# **空なら共有の日報データと同じフォルダ**(`KEY_ACCESS_DIR`)。どの端末からも
# 届く場所でないと、管理者が別のPCから直せないので。フォルダが無ければ作らず、
# 届くようになったら写します(`services/local_backup`)
KEY_BACKUP_DIR = "local_backup_dir"


# 音声ファイルの名前。鍵ごとに1つ(`logic/sound.SOUND_KEYS`)。
# **名前をコードに持たない** ── 現場で音を差し替えたいときに、
# コードを配り直さずに済ませるため
def sound_file_key(sound_key: str) -> str:
    return f"sound_file_{sound_key}"

# 設定画面から変えられる参照パス。**表示名はここが唯一の出どころ**
PATH_KEYS = (KEY_ACCESS_DIR, KEY_REFERENCE_DIR, KEY_WIP_DIR, KEY_MATERIAL_DIR,
             KEY_TRANSMISSION_DIR, KEY_SOUND_DIR,
             KEY_MONTHLY_DIR, KEY_REPORT_OUT_DIR, KEY_REPORT_OUT_DIR2,
             KEY_STANDARD_TIME_OUT_DIR,
             KEY_ACCESS_DB_FILE, KEY_LINE_TARGET_FILE, KEY_STOP_REASON_FILE,
             KEY_VC_MASTER_DIR, KEY_LOG_DIR, KEY_BACKUP_DIR)


def resolve_dir(text: str) -> Path:
    r"""打たれた道を、**実際に見に行く道**にする。

    2通りの書き方を受ける。

        絶対  ``\\サーバ\共有\…`` / ``C:\data\…`` / ``/mnt/share/…``
              打たれたまま使う
        相対  ``data\ref`` / ``..\共有``
              **アプリのフォルダから**たどる(``app_config.APP_ROOT``)

    相対を「いまの作業フォルダ」から見ないのが要点。作業フォルダは
    どこから起動したかで変わるので、同じ設定でも端末ごとに違う場所を
    指すことになる。アプリのフォルダなら、フォルダごとコピーして配る
    運用でも、写しの中の同じ場所を指し続ける。

    (``~`` は利用者のフォルダに開く。共有に届かない端末で、手元の
     写しを指すのに使える)
    """
    trimmed = (text or "").strip().strip('"')
    if not trimmed:
        raise ValueError("道が空です")
    path = Path(trimmed).expanduser()
    return path if path.is_absolute() else app_config.APP_ROOT / path


def is_relative_setting(text: str) -> bool:
    """その書き方は相対か。画面に「どちらとして読んだか」を出すため。"""
    trimmed = (text or "").strip().strip('"')
    if not trimmed:
        return False
    return not Path(trimmed).expanduser().is_absolute()


def _configured_dir(key: str, fallback: Path) -> Path:
    """設定画面の値があればそれ、無ければ `fallback`。

    `user_settings` を遅延importするのは循環参照を避けるため
    (あちらは `config` を読む)。
    """
    from . import user_settings

    configured = user_settings.get(key)
    if isinstance(configured, str) and configured.strip():
        try:
            return resolve_dir(configured)
        except ValueError:                            # pragma: no cover
            pass
    return fallback


@dataclass(frozen=True)
class Settings:
    # ------------------------------------------------------------------
    # Local SQLite database (the "day to day" store; source of truth for
    # the running shift, written to on every autosave).
    # ------------------------------------------------------------------
    app_dir: Path = field(default_factory=_default_app_dir)
    sqlite_filename: str = "nippou_local.sqlite3"

    # ------------------------------------------------------------------
    # Access (.accdb) bridge. NIPPOU_DB_FILE mirrors the VBA constant of
    # the same name (see NippouDB_GetDBPath). ACCESS_DIR mirrors
    # PATH_梱包_日報管理.
    # ------------------------------------------------------------------
    #
    # **置き場所は property で読む**(下の ``access_dir``)。dataclass の
    # 既定値は組み立てたときに1度決まるので、設定画面で変えても
    # `SETTINGS` を作り直すまで効かない ── 「保存したのに変わらない」の
    # もとになる。読むたびに設定を見に行く形にする
    # 既定のファイル名。実際に使う名前は property(``access_db_name``)で、
    # 設定画面から変えられる ── **拡張子で書き先の形式が決まる**
    #
    # **既定は sqlite3。** 上流の変換が Access から sqlite3 へ移り終わって
    # いるので、既定が `.accdb` のままだと「まず設定を直さないと動かない」
    # 状態から始まる。同じフォルダに `.accdb` しか無ければ `_pick_source`
    # がそちらを拾うので、まだ Access の現場もそのまま動く
    #
    # **名前は「日報データ」。** 現場に置かれている実ファイルがこの名前
    # (`T_日報ヘッダー_<ライン>` / `T_日報明細_<ライン>` が入っている)。
    # 旧名「日報管理」しか無いフォルダでも読めます(`OLDER_NAMES`)
    access_db_filename: str = "日報データ.sqlite3"
    #: 標準作業時間の蓄積先。**共有(日報データと同じフォルダ)に1つ**。
    #: 全ライン・全端末が同じファイルへ書く(`services/standard_time`)
    standard_time_filename: str = "標準作業時間.sqlite3"

    # Header/detail table name templates. ``{line}`` is replaced with a
    # sanitized line name, matching NippouDB_HeaderTable / _DetailTable.
    access_header_table_template: str = "T_日報ヘッダー_{line}"
    access_detail_table_template: str = "T_日報明細_{line}"
    # 集計フォーマットの3つ。**VBAには無い表** ── あちらは集計を Excel の
    # シートに書いていた。値で残すと決めたので、共有にも**手元と同じ
    # 3階層で**置く(ツールが無くても Excel や DB Browser で読める)
    access_summary_table_template: str = "T_日報集計_{line}"
    access_agg_detail_table_template: str = "T_日報集計明細_{line}"
    access_stop_detail_table_template: str = "T_日報停止明細_{line}"

    # cscript.exe / powershell.exe -- overridable for testing.
    cscript_path: str = "cscript.exe"
    powershell_path: str = "powershell.exe"
    subprocess_timeout_sec: int = 60

    # Retry policy, mirrors adoSQL's Const MAX_RETRY = 3 and
    # ``Sleep 1000 * retryCount``.
    max_retry: int = 3
    retry_base_wait_sec: float = 1.0

    # ------------------------------------------------------------------
    # Access接続の方式選択。
    #   "vbscript" (既定): 生成したVBScriptをcscript.exe経由で実行し、
    #     ADODBのBeginTrans/CommitTrans/RollbackTransで反映する。
    #     一時ファイル出力とプロセス起動のオーバーヘッドはあるが、
    #     長らく運用実績のあるVBA版と同じ経路なので既定にしている。
    #   "odbc": pip不要のctypes直結(dbkit.access_odbc)。サブプロセス
    #     起動が無い分軽いが、このリポジトリでの導入は新しく、実機の
    #     Windows+Accessドライバでの検証はまだ済んでいない。まず
    #     "vbscript" のまま様子を見て、動作確認ができてから切り替える
    #     運用を推奨する(README「Access接続方式の選択」参照)。
    # ------------------------------------------------------------------
    access_backend: str = field(default_factory=lambda: os.environ.get("NIPPOU_ACCESS_BACKEND", "vbscript"))

    # ------------------------------------------------------------------
    # Admin mode (chkAdminMode / AuthenticateAdmin)。
    # 合言葉は property(``admin_password``)。設定画面で変えられる
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Autosave (NippouDB_AutoSave: NDB_AUTOSAVE_INTERVAL_SEC throttle).
    # ------------------------------------------------------------------
    autosave_interval_sec: int = 60

    # 直の終わりのタイマー (VBA StartPrintReminderTimer: 1分ごとに回り、
    # 終了15分前から催促、5分前で自動実行)。**名前は VBA のままですが、
    # 5分前にやるのは印刷ではなく確定です** ── 紙は「印刷」ボタンから、
    # 作業者が欲しいときだけ出します(`services/shift_close.py`)。
    print_reminder_interval_sec: int = 60
    print_warning_minutes: int = 15
    auto_print_minutes: int = 5

    # 直の終わりを過ぎてから、入力画面が**見るだけ**になるまで(分)。
    #
    # **0 ── 終わりの時刻ちょうどで手が止まります。** はじめは 30分の
    # 猶予を置いていましたが、やめました:
    #
    #     「入力時間が足りなくなった場合しか得がない
    #       でもほとんどのケースは入力忘れ、保存忘れ
    #       時間が伸びることによって次の直が尻ぬぐいをすることになる」
    #
    # 得をするのは打ち終わらなかった直(まれ)で、損をするのは忘れて
    # 帰った直の後始末(よくある)です。そのぶん、閉じる**前**の催促
    # (15分前の声かけ・5分前の自動確定)を頼りにします。
    # 判断は `logic/shift_anchor.decide`。
    shift_grace_minutes: int = 0

    # ------------------------------------------------------------------
    # Sound files (see logic/sound.py). Paths are relative to sound_dir;
    # on non-Windows platforms playback is a logged no-op.
    # ------------------------------------------------------------------
    # 置き場所は property(``sound_dir``)。理由は ``access_dir`` と同じ

    # ------------------------------------------------------------------
    # GW計算フォーム(梱包資材重量計算)・人員フォームが参照する外部マスタ
    # Accessファイル。いずれも読み取り専用(①取込みのみ、③反映の対象外)。
    # PATH_仕掛_台帳 / PATH_AIM_参照 (VBA) に相当。
    #   gw_lot_master_filename   : LotNo検索 (材質/調質/板厚幅丈/用途コード等)
    #   gw_order_master_filename : オーダーNo検索 (VC表裏/合紙/製品単重等)
    #   gw_material_master_filename : 「資材重量」「VC重量」に加えて
    #     「班員名簿」（人員フォーム用）も同居する（提出データで確認済み）。
    # ------------------------------------------------------------------
    # 置き場所は property(``gw_reference_dir``)。理由は ``access_dir`` と同じ
    gw_shared_table_name: str = "仕掛"
    # **既定は sqlite3**(`access_db_filename` と同じ理由)。同じ名前の
    # `.accdb` しか無いフォルダでは `_pick_source` がそちらを拾う。
    # 旧名(SIKALOTNOW / SIKAODRNOW / SIKAHIKINOW)しか置かれていない
    # フォルダでも読める ── `OLDER_NAMES` を参照
    gw_lot_master_filename: str = "SIKALOT.sqlite3"
    gw_order_master_filename: str = "SIKAODR.sqlite3"
    # 引当。**ロットと受注をつなぐ唯一の橋**(VBA `SQLiteLot検索` の
    # `g_HikiData`)。ロット番号から受注番号を知る道はここしか無いので、
    # これが読めないと VC も単重も合紙も出ない
    gw_hiki_master_filename: str = "SIKAHIKI.sqlite3"
    gw_material_master_filename: str = "梱包資材マスタ.sqlite3"
    # VC長さ計算のマスタ(品種・VC厚・内径・定尺・早見表)。**無ければ初期値で
    # 作ります**(`nippou/vc/masters.py`)。vc-calculator の `vc_master.sqlite3`
    # を同じフォルダに置けば、そのまま読みます(`OLDER_NAMES`)
    vc_master_filename: str = "VC計算マスタ.sqlite3"
    gw_material_master_table: str = "資材重量"
    gw_vc_master_table: str = "VC重量"
    staff_master_table: str = "班員名簿"
    #: 班員名簿の「オペレーター」の列(AOP / ABOP / BOP。`logic/operator` ── v4.15.0)
    staff_operator_column: str = "オペレーター"
    # 包装仕様の注意表記(VBA `PackagingSpecificationNo`)。梱包資材マスタ
    # に同居している。**ロットを打った時点で出す** ── 打ち終わってから
    # 気づくと、荷を解くことになる
    pack_note_table: str = "注意_包装仕様"
    # コイルの割り数。**機側・NS1 のときだけ引く**(コイル形状。VBA は AIM も
    # 引いていた ── v4.21.0 で外した)。VBA はこのファイルだけ Access の
    # まま残していたが、ほかの参照と同じく sqlite3 として扱う
    gw_coil_master_filename: str = "LS4LOT.sqlite3"

    #: ライン毎の目標枚数(45度線)。**CSVが正**で、マスタ「ライン毎目標」は
    #: CSVと**両方**読む。同じラインならCSVが勝つ(`services/targets.py`)
    line_target_filename: str = "ライン毎目標.csv"
    #: 取り込み元のマスタの表
    line_target_table: str = "ライン毎目標"

    # ------------------------------------------------------------------
    # 伝送用ファイル。**停止理由と直の境界時刻の、両方の出どころ。**
    #
    # 実ファイルで確かめた中身:
    #   作業停止時間内訳_1 … 管理ロス(休憩食事/TPM活動・清掃/人員不足 …)
    #                         記号は 0〜6 の数字
    #   作業停止時間内訳_2 … 突発・待ち(突発停止(機械)/ﾌｫｰｸ待ち …)
    #                         記号は イロハ… の片仮名
    #   作業停止時間内訳_3 … ハンドリング(段取り変更/内径カット …)
    #                         記号は A〜G の英字
    #     いずれも列は「管理番号」「内訳」「内訳番号」「備考」
    #   時間用             … 直の境界時刻。列は「番号」「直」「開始」「終了」
    #                         「更新日」(1/2/3/昼 の4行)
    #   名簿               … バーコード付きの氏名簿
    #
    # **直の境界時刻をここから読むのは、日報管理ではないから。** 以前は
    # 日報管理(=書き込み先)から読んでいたが、時間用が置いてあるのは
    # 伝送用ファイルのほう
    # ------------------------------------------------------------------
    transmission_master_filename: str = "伝送用ファイル.sqlite3"
    stop_reason_tables: tuple[str, ...] = (
        "作業停止時間内訳_1", "作業停止時間内訳_2", "作業停止時間内訳_3")
    shift_time_table: str = "時間用"
    #: 停止内訳のCSV。**あれば上の3つの表より先に読む**(分類ごと)。
    #: 置き場所は設定画面の「停止内訳の置き場所」(既定: 参照用マスタと同じ)
    stop_reason_csv_filename: str = "停止内訳.csv"

    # ------------------------------------------------------------------
    # 参照パス。**読むたびに設定を見に行く**
    # ------------------------------------------------------------------
    @property
    def access_dir(self) -> Path:
        """日報管理.accdb の置き場所(``PATH_梱包_日報管理`` 相当)。"""
        return _configured_dir(
            KEY_ACCESS_DIR,
            _env_path("NIPPOU_ACCESS_DIR", str(Path.home() / "NippouAccess")))

    @property
    def gw_reference_dir(self) -> Path:
        """参照用マスタの置き場所(``PATH_仕掛_台帳`` / ``PATH_AIM_参照`` 相当)。"""
        return _configured_dir(
            KEY_REFERENCE_DIR,
            _env_path("NIPPOU_GW_REF_DIR", str(Path.home() / "NippouGwRef")))

    @property
    def monthly_dir(self) -> Path:
        """月替わりで1か月ぶんを書き出す先(VBA の ``自動集計`` フォルダ)。

        VBA は共有の `…\\日報管理\\自動集計\\yyyy.mm\\<ライン>` へ
        xlsx を保存していました。ここも**既定は共有側**(日報管理と同じ
        フォルダの下)にします ── 手元に出すと、端末を替えた人が
        前月を見られません。設定画面から変えられます。
        """
        from . import user_settings

        configured = user_settings.get(KEY_MONTHLY_DIR)
        if isinstance(configured, str) and configured.strip():
            try:
                return resolve_dir(configured)
            except ValueError:                        # pragma: no cover
                pass
        env = os.environ.get("NIPPOU_MONTHLY_DIR", "").strip()
        if env:
            return Path(env)
        return self.access_dir / "自動集計"

    def sound_file(self, sound_key: str) -> str:
        """その音のファイル名。設定画面の値を優先し、無ければ既定。

        **鳴らさないなら空**(設定で「鳴らさない」を選んだ・既定が鳴らさない
        出来事 ── `logic/sound.SOUND_OFF`)。
        """
        from . import user_settings
        from .logic.sound import SOUND_OFF, spec

        configured = user_settings.get(sound_file_key(sound_key))
        if isinstance(configured, str) and configured.strip():
            name = configured.strip()
            return "" if name == SOUND_OFF else name
        found = spec(sound_key)
        return found.default_file if found else ""

    def sound_path(self, sound_key: str) -> Optional[Path]:
        """その音の道。名前が決まらなければ ``None``。"""
        name = self.sound_file(sound_key)
        return (self.sound_dir / name) if name else None

    @property
    def sound_dir(self) -> Path:
        return _configured_dir(
            KEY_SOUND_DIR,
            _env_path("NIPPOU_SOUND_DIR",
                      str(Path.home() / "NippouApp" / "sounds")))

    @property
    def admin_password(self) -> str:
        """管理者モードの合言葉。

        **設定画面で変えられる**(`admin_password.py` が撹拌して持つ)。
        変えていなければこの既定値。VBA の ``Const`` と同じく、これは
        押し間違いを防ぐための関門であって、本来の意味での権限管理では
        ない ── 端末に触れる人はファイルを直接開ける。
        """
        return "nisk"

    # ------------------------------------------------------------------
    # 参照するファイル。**sqlite3 があればそちらを先に見る**
    # ------------------------------------------------------------------
    # 上流が Access から sqlite3 へ移りつつあり、置き換えはファイル単位で
    # 進む。同じフォルダに両方あるあいだは sqlite3 を採る ── 新しいほうが
    # 正で、共有のファイルを開かずに読める(`source_db` の説明)
    # 3つの置き場所(v4.16.0)。**空なら参照用マスタと同じフォルダ**
    @property
    def wip_master_dir(self) -> Path:
        """仕掛ロット・仕掛引当・仕掛受注(SIKALOT / SIKAHIKI / SIKAODR)のフォルダ。"""
        return _configured_dir(KEY_WIP_DIR, self.gw_reference_dir)

    @property
    def material_master_dir(self) -> Path:
        """梱包資材マスタ・コイル割り数(LS4LOT)のフォルダ。"""
        return _configured_dir(KEY_MATERIAL_DIR, self.gw_reference_dir)

    @property
    def transmission_master_dir(self) -> Path:
        """伝送用ファイルのフォルダ。"""
        return _configured_dir(KEY_TRANSMISSION_DIR, self.gw_reference_dir)

    @property
    def transmission_master_path(self) -> Path:
        """伝送用ファイル。停止理由内訳と時間用(直の境界)の両方が入っている。"""
        return _pick_source(self.transmission_master_dir, self.transmission_master_filename)

    # 停止理由を読む側から見た名前。**同じファイル**を指す
    @property
    def stop_reason_master_path(self) -> Path:
        return self.transmission_master_path

    @property
    def sqlite_path(self) -> Path:
        return self.app_dir / self.sqlite_filename

    @property
    def access_db_name(self) -> str:
        """③反映の書き先のファイル名。設定画面の値を優先する。"""
        from . import user_settings

        configured = user_settings.get(KEY_ACCESS_DB_FILE)
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return self.access_db_filename

    @property
    def access_db_path(self) -> Path:
        return _pick_source(self.access_dir, self.access_db_name)

    @property
    def standard_time_db_path(self) -> Path:
        """標準作業時間.sqlite3(共有の日報データと同じフォルダ)。"""
        return self.access_dir / self.standard_time_filename

    @property
    def gw_lot_master_path(self) -> Path:
        return _pick_source(self.wip_master_dir, self.gw_lot_master_filename)

    @property
    def gw_order_master_path(self) -> Path:
        return _pick_source(self.wip_master_dir, self.gw_order_master_filename)

    @property
    def gw_hiki_master_path(self) -> Path:
        return _pick_source(self.wip_master_dir, self.gw_hiki_master_filename)

    @property
    def gw_material_master_path(self) -> Path:
        return _pick_source(self.material_master_dir, self.gw_material_master_filename)

    @property
    def vc_master_dir(self) -> Path:
        """VC計算マスタのフォルダ。**空なら参照用マスタと同じ**(設定画面で変えられる)。"""
        return _configured_dir(KEY_VC_MASTER_DIR, self.gw_reference_dir)

    @property
    def vc_master_path(self) -> Path:
        """VC長さ計算のマスタ。**名前の優先は `VC計算マスタ` → `vc_master`**
        (`OLDER_NAMES`)。どちらも無ければ `VC計算マスタ.sqlite3`(初めて
        使うときに作る)。"""
        return _pick_source(self.vc_master_dir, self.vc_master_filename)

    def vc_master_candidates(self) -> list[Path]:
        """VC計算マスタとして探す名前を、**優先の順に**(画面で関係を見せるため)。"""
        stem = Path(self.vc_master_filename).stem
        return [self.vc_master_dir / f"{name}.sqlite3"
                for name in (stem, *OLDER_NAMES.get(stem, ()))]

    @property
    def gw_coil_master_path(self) -> Path:
        """コイルの割り数(`LS4LOT`)。機側・NS1 のときだけ読む。梱包資材マスタと同じフォルダ。"""
        return _pick_source(self.material_master_dir, self.gw_coil_master_filename)

    @property
    def line_target_path(self) -> Path:
        """ライン毎目標のCSV。**設定画面で置き場所ごと変えられる。**

        設定の書き方で、どこを見るかが決まります:

            空                    … 参照用マスタの置き場所 / 既定の名前
            ライン毎目標.csv      … 参照用マスタの置き場所 / その名前
            目標\\今期.csv         … アプリのフォルダからの相対
            \\\\server\\共有\\目標.csv … そのまま

        **フォルダと名前を2つの設定に分けません。** 分けると「名前だけ
        変えたい」ときに2か所を見ることになり、共有の別フォルダに置く
        ような使い方もできなくなります。
        """
        return self._configured_file(KEY_LINE_TARGET_FILE,
                                     self.line_target_filename)

    @property
    def stop_reason_csv_path(self) -> Path:
        """停止内訳のCSV。**書き方と既定はライン毎目標と同じ**
        (`line_target_path`)── 空なら参照用マスタの置き場所の `停止内訳.csv`。
        """
        return self._configured_file(KEY_STOP_REASON_FILE,
                                     self.stop_reason_csv_filename)

    def _configured_file(self, key: str, default_name: str) -> Path:
        """設定画面で置き場所ごと変えられる、読むだけのファイル。"""
        from . import user_settings

        configured = user_settings.get(key)
        text = configured.strip() if isinstance(configured, str) else ""
        if not text:
            return self.gw_reference_dir / default_name
        given = Path(text).expanduser()
        if given.is_absolute():
            return given
        # 区切りが入っていればアプリのフォルダからの相対、
        # 名前だけなら参照用マスタのフォルダの下
        if len(given.parts) > 1:
            return (app_config.APP_ROOT / given).resolve()
        return self.gw_reference_dir / given

    @property
    def log_dir(self) -> Path:
        """ログの置き場所。**設定画面から変えられます**(`KEY_LOG_DIR`)。

        決めていなければローカル領域の ``logs``(基盤仕様書 4.6)。
        """
        from . import user_settings

        configured = user_settings.get(KEY_LOG_DIR)
        text = configured.strip() if isinstance(configured, str) else ""
        return resolve_dir(text) if text else self.default_log_dir

    @property
    def default_log_dir(self) -> Path:
        """**この端末の中の**ログの置き場所。設定した先に書けないときの逃げ先で、
        起動の記録(launcher.log / guard.log)はいつもここ。"""
        override = _app_dir_override()
        return override / "logs" if override else app_config.local_dir("logs")

    # ------------------------------------------------------------------
    # 集計用CSV・印刷用HTMLの出力先(reporting/csv_export.py, reporting/
    # print_format.py)。
    # ------------------------------------------------------------------
    @property
    def report_output_dir(self) -> Path:
        """CSVと印刷用HTMLの置き場所。**設定画面から変えられます。**

        既定はローカル領域の ``work`` ── 印刷用HTMLは開き直せば作り直せる
        ので、消えても困らない領域に置いてあります。

        ただし**集計CSVは人が開くもの**で、「どこに出たのか分からない」と
        いう声がありました。共有のフォルダを指せば、出したCSVをそのまま
        他の人が開けます(`user_settings` の値が最優先)。
        """
        from . import user_settings

        configured = user_settings.get(KEY_REPORT_OUT_DIR)
        text = configured.strip() if isinstance(configured, str) else ""
        if text:
            return resolve_dir(text)
        override = _app_dir_override()
        return override / "reports" if override else app_config.local_dir("work")

    @property
    def report_output_dir_2(self) -> Optional[Path]:
        """集計CSVの2つ目の出力先。**決めていなければ None**(出さない)。"""
        from . import user_settings

        configured = user_settings.get(KEY_REPORT_OUT_DIR2)
        text = configured.strip() if isinstance(configured, str) else ""
        return resolve_dir(text) if text else None

    @property
    def standard_time_output_dir(self) -> Path:
        """標準作業時間のCSVの置き場所。**決めていなければ集計CSVと同じ先。**"""
        from . import user_settings

        configured = user_settings.get(KEY_STANDARD_TIME_OUT_DIR)
        text = configured.strip() if isinstance(configured, str) else ""
        return resolve_dir(text) if text else self.report_output_dir

    #: 控えのフォルダの名前(この下にライン名のフォルダ)
    local_backup_folder: str = "LocalBackup"
    #: 控えのファイルの名前(ライン名のフォルダの中)
    local_backup_filename: str = "日報入力データ.sqlite3"

    @property
    def local_backup_base(self) -> Path:
        """控えを置くフォルダの**親**(設定の値。空なら共有の日報データと同じ)。"""
        from . import user_settings

        configured = user_settings.get(KEY_BACKUP_DIR)
        text = configured.strip() if isinstance(configured, str) else ""
        return resolve_dir(text) if text else self.access_dir

    @property
    def local_backup_root(self) -> Path:
        """`<控えの置き場所>\\LocalBackup`。"""
        return self.local_backup_base / self.local_backup_folder

    @property
    def summary_csv_dirs(self) -> list[Path]:
        """集計CSVを出す先ぜんぶ。**1つ目が先頭**(2つ目は決めてあるときだけ)。

        2つ目が1つ目と同じ道なら1つにまとめます ── 同じ所へ2回書いて、
        知らせに同じ道が2行並ぶだけになるので。
        """
        dirs = [self.report_output_dir]
        second = self.report_output_dir_2
        if second is not None and second != dirs[0]:
            dirs.append(second)
        return dirs


# ----------------------------------------------------------------------
# 参照DBの旧名。**名前が変わる日を跨ぐための橋。**
#
# 上流の書き出しが `SIKALOTNOW` → `SIKALOT` のように変わる。両方が同時に
# 切り替わることはまず無いので、新しい名前が置かれるまでのあいだは旧名を
# 読む。逆(先に新しい名前が来る)も同じで、置いてあるほうを読む。
#
# **設定画面を触らせないための表**であって、恒久の別名ではない。旧名の
# ファイルが現場から消えたら、この表からも消してよい。
# ----------------------------------------------------------------------
OLDER_NAMES: dict[str, tuple[str, ...]] = {
    "SIKALOT": ("SIKALOTNOW",),
    "SIKAODR": ("SIKAODRNOW",),
    "SIKAHIKI": ("SIKAHIKINOW",),
    # 書き先の日報。**ここだけは中身が消えない**ようにしたいので、
    # 旧名のファイルがあればそちらへ書き続ける(新名で作り直すと、
    # これまでのぶんが入っていない空のファイルができる)
    "日報データ": ("日報管理",),
    # VC長さ計算のマスタ。vc-calculator(単独のツール)が作った `vc_master`
    # があればそれを使う ── 表の形は同じで、2つのツールで1つを共有できる
    "VC計算マスタ": ("vc_master",),
}


def _pick_source(directory: Path, filename: str) -> Path:
    """そのフォルダで実際に使うファイル。**sqlite3 を先に見る。**

    上流の変換は AIM(Access)から sqlite3 へ、ファイル単位で移っていく。
    切り替えの日に設定を触らせないため、同じ名前の ``.sqlite3`` / ``.db``
    が置かれていればそちらを採る。無ければ渡された名前のまま
    (= これまでどおり Access)を返す。

    名前そのものが変わる場合(参照DBの ``…NOW`` が取れた)も同じ考えで、
    新しい名前が無ければ ``OLDER_NAMES`` の旧名を探す。**上流が新しい
    名前で書き出すのを待たずに配れる**ようにするため。

    **見つからなくても、その道を返す。** 「ファイルがありません」は
    読もうとした側が言うほうが、どのファイルを探して無かったのかまで
    伝わる。返すのは設定されている(=新しい)名前なので、文言にも
    そちらが出る。
    """
    stem = Path(filename).stem
    for name in (stem, *OLDER_NAMES.get(stem, ())):
        for suffix in (".sqlite3", ".db"):
            candidate = directory / f"{name}{suffix}"
            try:
                if candidate.exists():
                    return candidate
            except OSError:                           # 共有に届かない
                return directory / filename
    return directory / filename


SETTINGS = Settings()
