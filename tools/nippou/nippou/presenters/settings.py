"""設定画面の中身を組み立てる

**業務の判断はここにある。** 画面(`app/routes/settings.py`)は受け取った
形をそのまま描くだけで、「保存してよいか」「何が要るか」は決めない。

【この画面が持つもの】
    参照パス      … どのフォルダを見に行くか。**変えるには管理者パスワード**
    マスタ管理    … そのフォルダに何があり、読めるかどうか
    管理者        … 過去データの呼び出し、パスワードの変更
    共有への保存  … 日報管理へ書き写す(③反映)・時間マスタの取り込み

【面(タブ)の並びもここが決める】
14枚のカードが1本に積まれていて、「機能を確かめたいのに、どこに何が
あるか分からない」状態でした(`TABS`)。**置き場所の欄は、それを使う
機能と同じ面に置きます** ── 「音の置き場所」と「音」が別のところに
あると、片方だけ直して帰ることになります。

鍵(管理者パスワード)の要否も面ごとにここが持ちます。**押してから
断られるのは手戻り**なので、面を開いた時点で見えるようにします。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import admin_password, config, distribution, source_db, user_settings
from ..config import SETTINGS
from ..logging_setup import get_logger

log = get_logger("presenters.settings")

# 断りの種類。**文言から推し量らない**
REFUSE_BAD_INPUT = "bad_input"
REFUSE_NEED_PASSWORD = "need_password"


# 画面に出す参照パスの並び。**表示名の出どころはここ1つ**
PATH_FIELDS = (
    (config.KEY_ACCESS_DIR, "共有の日報管理のパス",
     "「共有へ保存」がここへ書きに行きます。みんなが見る日報データ"
     "(日報管理)を置くフォルダです"),
    (config.KEY_ACCESS_DB_FILE, "日報データのファイル名",
     "拡張子で書き先が決まります。.sqlite3 なら Windows もドライバも要らず、"
     "無ければ初回の保存で作られます(既定: 日報データ.sqlite3)"),
    (config.KEY_REFERENCE_DIR, "参照用マスタの参照パス",
     "下の3つ(仕掛・梱包資材マスタ・伝送用ファイル)を空にしておくと、このフォルダを見ます。"
     "ライン毎目標・停止内訳・VC計算マスタの既定の置き場所でもあります"),
    # 置き場所ごとに分ける(v4.16.0)。**空なら参照用マスタと同じフォルダ**
    (config.KEY_WIP_DIR, "仕掛ロット・引当・受注の置き場所",
     "仕掛ロット(SIKALOT)・仕掛引当(SIKAHIKI)・仕掛受注(SIKAODR)があるフォルダ"
     "(既定: 参照用マスタと同じフォルダ)"),
    (config.KEY_MATERIAL_DIR, "梱包資材マスタの置き場所",
     "梱包資材マスタ と コイル割り数(LS4LOT)があるフォルダ"
     "(既定: 参照用マスタと同じフォルダ)"),
    (config.KEY_TRANSMISSION_DIR, "伝送用ファイルの置き場所",
     "伝送用ファイル(停止理由内訳・直の境界時刻)があるフォルダ"
     "(既定: 参照用マスタと同じフォルダ)"),
    (config.KEY_SOUND_DIR, "音声ファイルの参照パス",
     "印刷の催促などで鳴らす音。無くても動きます"),
    (config.KEY_MONTHLY_DIR, "月別書き出しの出力パス",
     "月が替わったとき、1か月ぶんを ライン名/yyyy.mm の下へ書き出します"
     "(既定: 日報データと同じフォルダの「自動集計」)"),
    (config.KEY_REPORT_OUT_DIR, "集計CSV・印刷用HTMLの出力パス",
     "「集計CSVを出力」で書き出す先。この下に ライン名/集計(印刷)/年月/日 "
     "のフォルダを作ります。共有のフォルダを指せば、出したCSVをそのまま"
     "他の人が開けます(既定: この端末の作業用フォルダ)"),
    (config.KEY_REPORT_OUT_DIR2, "集計CSVの出力パス(2つ目)",
     "「共有へ保存」で送れたときに、送った日の集計CSV(3本)と月別の書き出し"
     "(この下の「自動集計」)をここへ出します。空なら出しません。見る人と"
     "アクセス権で分けたいときに(例: 1つ目は現場の共有、2つ目は管理の人"
     "だけが入れるフォルダ)。印刷用HTMLは出しません"),
    (config.KEY_STANDARD_TIME_OUT_DIR, "標準作業時間CSVの出力パス",
     "標準作業時間の画面の「CSVに出す」(標準の表・同条件の作業)で書き出す先。"
     "このフォルダの直下に 標準作業時間_日付_ライン.csv / 同条件作業_日付_…csv "
     "を出します"
     "(空なら集計CSV・印刷用HTMLの出力パスと同じ)"),
    (config.KEY_LOG_DIR, "ログの出力パス",
     "動いた記録(nippou.log)と、エラー・操作の記録(出来事_日付_端末.jsonl)・"
     "なぜなぜ分析を書く先。共有のフォルダを指せば、どの端末で起きたエラーも"
     "設定・管理者の「ログ」の面で1つの一覧に並びます(ファイル名に端末の名前が"
     "入るので混ざりません)。届かないときはこの端末の logs へ書きます"
     "(既定: この端末の logs)"),
    (config.KEY_BACKUP_DIR, "日報入力データの控えの置き場所",
     "保存のたびに、手元と同じページをこの下の LocalBackup\\ライン名\\ へも書きます"
     "(手元と両方)。手元に無いページはここから戻します。アクセス権限の表で"
     " Administrator のPCは、ここにある全ラインの日報を「記録を見る」で見て直せます"
     "(既定: 共有の日報管理のパスと同じフォルダ)"),
    (config.KEY_LINE_TARGET_FILE, "ライン毎目標の置き場所",
     "グラフの45度線に使う目標枚数のCSV(ライン毎目標.csv)を置くフォルダ。"
     "共有のフォルダを指せば、全端末が同じ目標を読みます"
     "(既定: 参照用マスタと同じフォルダ)"),
    (config.KEY_STOP_REASON_FILE, "停止内訳の置き場所",
     "停止理由の一覧のCSV(停止内訳.csv)を置くフォルダ。あれば伝送用ファイルの"
     "表より先に読みます(分類ごと)。共有のフォルダを指せば、全端末が同じ一覧を"
     "使います(既定: 参照用マスタと同じフォルダ)"),
    (config.KEY_VC_MASTER_DIR, "VC計算マスタの置き場所",
     "VC長さ計算のマスタを置くフォルダ。VC計算マスタ.sqlite3 があればそれ、"
     "無ければ vc-calculator の vc_master.sqlite3 を読みます(どちらも無ければ"
     "初めて使うときに VC計算マスタ.sqlite3 を作ります)。vc-calculator と同じ"
     "フォルダを指せば、2つのツールで1つのマスタを使えます"
     "(既定: 参照用マスタと同じフォルダ)"),
)


# ------------------------------------------------------------------
# 管理者パスワードで守る設定 ── **パスは全部**
#
# 【なぜ全部なのか】
# 参照パスを変えると、**このツールが読み書きする相手そのもの**が変わります。
# 間違った先を指したまま使うと、GW計算も人員も全停も、黙って別のデータで
# 動きます ── 画面はふつうに出るので、気づくのは製品が出たあとです。
#
# はじめは「別のファイルを**読ませる**ところまで届く設定」だけを守って
# いました(共有の日報管理 / 参照用マスタ / 日報データのファイル名)。
# 残りは間違えても音が鳴らない・CSVが別の場所に出るだけだから、という
# 理由です。**それでも守ることにしました**(「パスの変更は全部パスワードを
# 必須にしておいてください」)。
#
# 書き出す先も、間違えれば**出したはずのものが誰にも見えません。**
# 月別の書き出しも集計CSVも、出た先を確かめる人は普段いません ──
# 気づくのは「先月ぶんが無い」と言われたときです。読む側と書く側で
# 守り方を変える理由は、そちらの側に無いということでした。
#
# 守るのは**パスだけ**です。音のファイル名(`SOUND_FILE_KEYS`)は
# ここに入れません ── 間違えても鳴らないだけで、読み書きの相手は
# 変わりません。
# ------------------------------------------------------------------
PROTECTED_LABELS = {key: label for key, label, _ in PATH_FIELDS}


# ------------------------------------------------------------------
# 面(タブ)
#
# 【なぜ分けるのか】
# 14枚のカードが1本に積まれていて、「機能を確かめたいのに、どこに何が
# あるか分からない」状態でした。縦に積んだものは下にあるほど**無いもの
# として扱われます** ── スクロールしないと見えないということは、
# 見えていないあいだは思い出せないということです。
#
# 【どう分けたのか ── 置き場所は、それを使う機能と同じ面に置く】
# 「置き場(参照パス)」と「その置き場を使う機能」が別々の面にあると、
# 片方だけ直して帰ることになります。だから面は**やることで切り**、
# パスの欄はその面の頭へ持っていきます:
#
#     音 → 音の置き場所 / 共有へ保存 → 日報データの置き場所
#     表を見る・直す → 参照用マスタの置き場所・ライン毎目標のファイル
#
# 【鍵(管理者パスワード)】
# **全部に要るわけではありません。** 要るのは「別のファイルを読ませる
# ところまで届く設定」(`PROTECTED_LABELS`)と、管理者モードの内側に
# 置いた操作だけです。どの面の何に要るのかを `lock` に1文で書き、
# 面を開いた時点で出します ── 押してから断られるのは手戻りなので。
# ------------------------------------------------------------------
@dataclass(frozen=True)
class TabSpec:
    """設定画面の面1つ。"""

    key: str
    label: str
    #: 何をする面か。見出しの隣に出す
    note: str
    #: 鍵が要る操作。**空なら鍵は要りません**(帯そのものを出さない)
    lock: str = ""


#: 面の並び。**触る頻度の順**(上ほどよく触る)。
TABS: tuple[TabSpec, ...] = (
    TabSpec("output", "保存と出力",
            "共有の日報管理へ保存・月替わりの書き出し",
            "共有の日報管理のパスを変えるとき"),
    TabSpec("master", "マスタ",
            "参照するファイルの状態と中身・ライン毎目標(45度線)・停止内訳・直の境界時刻",
            "マスタの中身を直すとき"),
    TabSpec("sound", "音", "出来事ごとに何を鳴らすか(鳴らさない/音声ファイル)"),
    TabSpec("data", "取り込みと直し",
            "過去の日報を取り込む・当直をDBから復旧・集計を作り直す・ライン名のコンバート",
            "この面の操作(取り込み・復旧・コンバート)ぜんぶ"),
    TabSpec("paths", "参照設定",
            "読みに行くフォルダと、書き出すフォルダ。パスはここだけで決めます",
            "パスを変えるとき(どれでも)"),
    TabSpec("terminal", "この端末と配布",
            "管理者モード・この端末のライン・この端末に残すファイル・配布設定・版と置き場所",
            "この端末のラインを変えるとき・配布設定を書き出すとき"),
    # 困ったときに開く面。**鍵は要りません**(見るのも、なぜなぜを書くのも、
    # その場に居る人がやること)
    TabSpec("logs", "ログ",
            "エラーと操作の記録・1件ずつのなぜなぜ分析・CSV"),
)
DEFAULT_TAB = "output"

#: 参照パスの欄を、どの面に置くか。
#:
#: 【なぜ1枚にまとめ直したのか】
#: 一度は「置き場は、それを使う機能と同じ面へ」で散らしました。実際に
#: 触ってもらうと**逆でした** ── 「カテゴリで分けてあるのはいいけど、
#: 設定の仕方が全部違うじゃん」。面ごとに保存ボタンがあり、欄の並びも
#: 揃っていないので、**同じ種類の操作なのに毎回やり方を探し直す**ことに
#: なっていました。
#:
#: パスを決めるのは**据え付けのときの1つの作業**です。作業が1つなら
#: 場所も1つ ── `参照設定` の面にまとめ、同じ形で並べます。
PATH_TAB: dict[str, str] = {
    config.KEY_ACCESS_DIR: "paths",
    config.KEY_REFERENCE_DIR: "paths",
    config.KEY_WIP_DIR: "paths",
    config.KEY_MATERIAL_DIR: "paths",
    config.KEY_TRANSMISSION_DIR: "paths",
    config.KEY_SOUND_DIR: "paths",
    config.KEY_MONTHLY_DIR: "paths",
    config.KEY_REPORT_OUT_DIR: "paths",
    config.KEY_REPORT_OUT_DIR2: "paths",
    config.KEY_STANDARD_TIME_OUT_DIR: "paths",
    config.KEY_LOG_DIR: "paths",
    config.KEY_BACKUP_DIR: "paths",
    # ファイル名の2つは**画面に出しません**(`SHOWN_PATH_KEYS`)。
    # それでも困りごとはどこかの面で言う必要があるので、面は決めます
    # ── 言わないと、見出しの数と下に並ぶ文言が食い違います
    config.KEY_ACCESS_DB_FILE: "paths",
    config.KEY_LINE_TARGET_FILE: "paths",
    config.KEY_STOP_REASON_FILE: "paths",
    config.KEY_VC_MASTER_DIR: "paths",
}

#: 参照設定の面に、この順で出す欄。**フォルダだけです。**
#:
#: 【なぜファイル名の欄を出さないのか】
#: 「ファイル名までの入力は不要にしてください。音だけファイル名の設定が
#: できるでいい」── そのとおりで、残り2つは**既定で足ります**:
#:
#:   日報データのファイル名   … 既定は `日報データ.sqlite3`。同じ名前の
#:                              `.accdb` しか無いフォルダでは、そちらを
#:                              自動で拾います(`config.py`)
#:   ライン毎目標のファイル   … 既定は `ライン毎目標.csv`。参照用マスタの
#:                              フォルダの下を見ます
#:
#: どちらも**設定そのものは残して**あります(`user_config.json` に値が
#: 入っていれば効きますし、APIも受け付けます)。画面に出さないだけです
#: ── 出しておくと、据え付けのたびに「ここは何を入れるのか」を考える
#: ことになり、既定のままでよいことが伝わりません。
SHOWN_PATH_KEYS: tuple[str, ...] = (
    config.KEY_REFERENCE_DIR,
    # 置き場所ごとに分ける(v4.16.0)。空なら参照用マスタと同じ
    config.KEY_WIP_DIR,
    config.KEY_MATERIAL_DIR,
    config.KEY_TRANSMISSION_DIR,
    config.KEY_SOUND_DIR,
    # ライン毎目標のCSVは**フォルダだけ**を出します(名前は決め打ち)。
    # 出していなかったあいだ、置き場所を変える道が画面に1つも無く、
    # **参照用マスタと同じフォルダにしか置けません**でした ── 目標は
    # マスタとは別の人が別の周期で直すものなので、そこに縛る理由が
    # ありません(`FIXED_NAME_KEYS`)
    config.KEY_LINE_TARGET_FILE,
    # 停止内訳のCSVも同じ(名前は決め打ち、フォルダだけ)。停止理由は
    # マスタ管理の鍵を持たない班長が直すものなので、参照用マスタとは
    # 別の(現場が書ける)フォルダに置けるようにする
    config.KEY_STOP_REASON_FILE,
    # VC計算マスタは vc-calculator と分け合うことがあるので、別のフォルダを
    # 指せるようにする(VC長さ計算の「設定」の面からも変えられる)
    config.KEY_VC_MASTER_DIR,
    config.KEY_ACCESS_DIR,
    config.KEY_MONTHLY_DIR,
    config.KEY_REPORT_OUT_DIR,
    config.KEY_REPORT_OUT_DIR2,
    config.KEY_STANDARD_TIME_OUT_DIR,
    config.KEY_LOG_DIR,
    config.KEY_BACKUP_DIR,
)

#: **フォルダだけを見せて、名前はこちらで付ける欄。**
#:
#: 「ファイル名までの入力は不要にしてください」と言われています。
#: かといって置き場所まで固定すると、参照用マスタと同じフォルダにしか
#: 置けません。そこで**画面はフォルダ、設定はフルパス**にします ──
#: 保存のときに名前を足すので、設定そのものは1つのままです
#: (`config.SETTINGS.line_target_path` の「フォルダと名前を2つの設定に
#: 分けません」を崩さない)。
FIXED_NAME_KEYS: dict[str, str] = {
    config.KEY_LINE_TARGET_FILE: "ライン毎目標.csv",
    config.KEY_STOP_REASON_FILE: "停止内訳.csv",
}

#: 参照設定の面での区切り。**読みに行く先と、書き出す先を分ける。**
PATH_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("read", "読みに行く先",
     (config.KEY_REFERENCE_DIR, config.KEY_WIP_DIR, config.KEY_MATERIAL_DIR,
      config.KEY_TRANSMISSION_DIR, config.KEY_SOUND_DIR,
      config.KEY_LINE_TARGET_FILE, config.KEY_STOP_REASON_FILE,
      config.KEY_VC_MASTER_DIR)),
    ("write", "書き出す先",
     (config.KEY_ACCESS_DIR, config.KEY_MONTHLY_DIR,
      config.KEY_REPORT_OUT_DIR, config.KEY_REPORT_OUT_DIR2,
      config.KEY_STANDARD_TIME_OUT_DIR, config.KEY_LOG_DIR,
      config.KEY_BACKUP_DIR)),
)

#: 画面が参照パスを名指しするときの短い名前。
#:
#: テンプレートに `"access_dir"` のような**生の設定キーを書かない**ため
#: です ── 設定キーが変わったとき、直す場所がここ1つで済みます。
PATH_KEY_NAMES: dict[str, str] = {
    "access_dir": config.KEY_ACCESS_DIR,
    "access_db_file": config.KEY_ACCESS_DB_FILE,
    "reference_dir": config.KEY_REFERENCE_DIR,
    "wip_dir": config.KEY_WIP_DIR,
    "material_dir": config.KEY_MATERIAL_DIR,
    "transmission_dir": config.KEY_TRANSMISSION_DIR,
    "sound_dir": config.KEY_SOUND_DIR,
    "monthly_dir": config.KEY_MONTHLY_DIR,
    "report_out_dir": config.KEY_REPORT_OUT_DIR,
    "report_out_dir_2": config.KEY_REPORT_OUT_DIR2,
    "standard_time_out_dir": config.KEY_STANDARD_TIME_OUT_DIR,
    "line_target_file": config.KEY_LINE_TARGET_FILE,
    "stop_reason_file": config.KEY_STOP_REASON_FILE,
    "vc_master_dir": config.KEY_VC_MASTER_DIR,
    "log_dir": config.KEY_LOG_DIR,
    "local_backup_dir": config.KEY_BACKUP_DIR,
}

#: ファイルの状態を、どの面で出すか(困りごとの数を面へ割り振るため)
FILE_TAB: dict[str, str] = {
    "access_db": "output",
    "lot": "master", "hiki": "master", "order": "master",
    "material": "master", "coil": "master", "transmission": "master",
    "vc": "master",
}

# そのフォルダにあるはずのファイル。マスタ管理の面がこれを1つずつ確かめる。
#
# **書き込む先はここだけ。** ほかは全部読むだけなので、扱いを分ける
#(写しを作らない・排他の見かたが違う・表が無ければ作る)
WRITE_TARGET_KEY = "access_db"

# 各ファイルを「何に使っているか」。**表と機能の対応をここに1か所だけ持つ**
# ── どのファイルのどの表が、画面のどの機能を動かしているのかが
# 設定画面から読めないと、参照パスを直しても直った気がしない
FILE_USES: dict[str, tuple[tuple[str, str], ...]] = {
    "access_db": (("T_日報ヘッダー_* / T_日報明細_*", "共有へ保存(書き込み先)"),),
    "lot": (("仕掛", "ロット検索(材・調質/寸法/検入枚数/合紙) と GW計算"),),
    # 画面にそのまま出る文字です。`**…**` と書くとアスタリスクが見えます
    "hiki": (("仕掛", "ロット番号 → 受注番号(引当)。ここが唯一の橋です"),),
    "order": (("仕掛", "受注からの自動選択(ＶＣ/合紙/単重/EX) と GW計算"),),
    "material": (
        ("資材重量", "GW計算の資材重量"),
        ("VC重量", "GW計算のVC重量"),
        ("班員名簿", "作業者管理(担当者の選択)"),
        ("アクセス権限",
         "このPCのライン・Administrator(ログインIDとPC名がこのPCと同じ行の「権限」。"
         "有効が0の行は読まない)"),
        ("注意_包装仕様",
         "包装仕様の注意表記(ロットを打った時点で etc 欄へ。コイルは画面に出す)"),
    ),
    "coil": (("仕掛", "コイル縦割・横縦割(機側 / NS1 のときだけ引く)"),),
    "vc": (
        ("VC品種", "VC長さ計算の品種・VC厚・内径"),
        ("VC内径選択肢", "内径を選ぶ品種の選択肢(大/小)"),
        ("枚数定尺", "枚数を出す製品の丈"),
        ("早見表ブロック / 早見表値", "VC長さ計算の早見表"),
        ("アプリ設定", "早見表の見出し・丸め・肉厚の逆算"),
    ),
    "transmission": (
        # 停止内訳.csv に行がある分類は、そちらが先(`logic/stop_csv`)
        ("作業停止時間内訳_1",
         "停止時間メニュー(管理ロス)。停止内訳.csv に 1 の行があればそちらを使う"),
        ("作業停止時間内訳_2",
         "停止時間メニュー(突発・待ち)。停止内訳.csv に 2 の行があればそちらを使う"),
        ("作業停止時間内訳_3",
         "停止時間メニュー(ハンドリング)。停止内訳.csv に 3 の行があればそちらを使う"),
        ("時間用", "直時間管理(直の境界時刻)"),
        ("用途名負荷係数算出",
         "負荷係数(コイルのラインの ﾛｯﾄ数・係数Lot数)"),
    ),
}

EXPECTED_FILES = {
    config.KEY_ACCESS_DIR: (
        ("access_db", "日報管理(共有へ保存する先)",
         lambda: SETTINGS.access_db_path),
    ),
    # 置き場所ごとに3つ(v4.16.0。空なら参照用マスタと同じフォルダ)
    config.KEY_WIP_DIR: (
        ("lot", "仕掛ロット", lambda: SETTINGS.gw_lot_master_path),
        ("hiki", "仕掛引当", lambda: SETTINGS.gw_hiki_master_path),
        ("order", "仕掛受注", lambda: SETTINGS.gw_order_master_path),
    ),
    config.KEY_MATERIAL_DIR: (
        ("material", "梱包資材マスタ",
         lambda: SETTINGS.gw_material_master_path),
        ("coil", "コイル割り数(LS4LOT)",
         lambda: SETTINGS.gw_coil_master_path),
    ),
    config.KEY_TRANSMISSION_DIR: (
        ("transmission", "伝送用ファイル",
         lambda: SETTINGS.transmission_master_path),
    ),
    # 置き場所を別に持つ(空なら参照用マスタと同じフォルダ)
    config.KEY_VC_MASTER_DIR: (
        ("vc", "VC計算マスタ", lambda: SETTINGS.vc_master_path),
    ),
}

#: **初めて使うときにこちらで作る**ファイル。無いこと自体は困りごとにしない
#: (VC計算マスタは VC長さ計算を開いたときに初期値で作る ── `nippou/vc`)
CREATED_ON_USE: frozenset[str] = frozenset({"vc"})

# どのファイルが、どのフォルダ設定から来ているか。画面がこれを出す
FILE_SOURCE_KEY = {
    key: dir_key
    for dir_key, group in EXPECTED_FILES.items()
    for key, _label, _getter in group
}


@dataclass
class PathView:
    """参照パス1つぶんの、画面に出す全部。"""

    key: str
    label: str
    note: str
    value: str = ""            # 設定に入っている文字(空なら既定を使っている)
    resolved: str = ""         # 実際に見に行く道
    is_default: bool = True
    # **配布先の値**で動いている(この端末で空、または配布先と同じ値)
    from_site: bool = False
    is_relative: bool = False
    exists: bool = False
    protected: bool = False
    # フォルダではなくファイル名の欄(フォルダを選ぶボタンを出さない)
    is_file: bool = False
    # どの面に置くか(`PATH_TAB`)
    tab: str = ""
    # **何を探しているのか。**
    #
    # 「見つかりません」とだけ出していて、「何が見つからないのか」が
    # 読めませんでした ── 欄によって見ているものが違います:
    #   フォルダの欄     … そのフォルダ
    #   ファイル名の欄   … 置き場所のフォルダ(ファイルは初回の保存で作る)
    #   読むファイルの欄 … そのファイルそのもの
    # (画面に出る文言は `found_text` / `missing_text`。`vars()` で渡すので、
    #  property ではなく値として持つ)
    looking_for: str = "フォルダ"
    found_text: str = ""
    missing_text: str = ""
    #: **使っていない**(空なら出さない欄で、空のまま)。既定の道を探しに
    #: 行かないので、「ありません」の赤い札も出さない(`OFF_WHEN_EMPTY_KEYS`)
    off: bool = False
    #: 欄が空のときに薄く出す文。**空が何を意味するか**は欄で違う
    #: (既定を使う / 出さない)
    empty_text: str = "既定を使います"


@dataclass
class FileView:
    """参照するファイル1つの状態。**読めるかどうかまで見る。**"""

    key: str
    label: str
    path: str = ""
    name: str = ""
    kind: str = ""             # "sqlite3" / "access" / ""
    exists: bool = False
    size: int = 0
    readable: bool = False
    copied_to: str = ""        # 手元の写し(読むだけの sqlite3 のときだけ)
    copied_at: str = ""
    tables: list[str] = field(default_factory=list)
    error: str = ""
    # **ここへ書くか。** ③反映の書き込み先(日報管理)だけ True
    is_write_target: bool = False
    # 書き込み先が sqlite3 なら、ドライバ無しで書ける
    writable: bool = False
    # このファイルが、どのフォルダ設定から来ているか。**「参照パスを直した
    # のに、どのファイルに効いたのか分からない」を無くす**
    source_key: str = ""
    source_label: str = ""
    directory: str = ""
    # 「この表が、画面のどの機能を動かしているか」。設定を直す人が
    # 見ているのは機能の側なので、表の名前だけでは繋がらない
    uses: list[dict[str, str]] = field(default_factory=list)


def path_views() -> list[PathView]:
    """参照パスの現状。**「設定した値」と「実際に見る道」を両方出す。**

    片方だけだと、相対で書いたときにどこを見ているのか分からず、
    「設定したのに見つからない」の原因が追えない。
    """
    stored = user_settings.load_all()
    out: list[PathView] = []
    for key, label, note in PATH_FIELDS:
        raw = stored.get(key)
        text = raw.strip() if isinstance(raw, str) else ""
        resolved = _resolved_dir(key)
        is_file = key in FILE_KEYS
        if key in FIXED_NAME_KEYS:
            # **画面はフォルダ。** 名前は保存のときにこちらで足すので、
            # 欄に出すのも、フォルダを選ぶボタンを出すのも親のほう
            # (`FIXED_NAME_KEYS`)
            is_file = False
            text = str(Path(text).parent) if text else ""
            looking_for = "ファイル"
        elif key in READ_FILE_KEYS:
            looking_for = "ファイル"
        elif is_file:
            looking_for = "置き場所のフォルダ"
        else:
            looking_for = "フォルダ"
        if key in OFF_WHEN_EMPTY_KEYS and not text:
            out.append(PathView(
                key=key, label=label, note=note, tab=PATH_TAB.get(key, ""),
                protected=key in PROTECTED_LABELS, off=True,
                empty_text="空なら出しません",
                resolved="(使っていません ── 1つ目だけに出します)"))
            continue
        view = PathView(
            key=key, label=label, note=note, value=text,
            resolved=str(resolved),
            is_default=not text,
            from_site=user_settings.origin(key) == user_settings.ORIGIN_SITE,
            is_relative=False if is_file else config.is_relative_setting(text),
            protected=key in PROTECTED_LABELS,
            is_file=is_file,
            tab=PATH_TAB.get(key, ""),
            looking_for=looking_for,
            empty_text=EMPTY_TEXT.get(key, "既定を使います"),
            found_text=f"この{looking_for}はあります",
            missing_text=f"この{looking_for}がありません")
        try:
            if key in READ_FILE_KEYS:      # `FIXED_NAME_KEYS` もここに入る
                # **読むだけのファイルは、そのファイルがあるかで見る。**
                # 置き場所だけ合っていても、CSVが無ければ目標線は出ない
                view.exists = resolved.is_file()
            elif is_file:
                # ファイル名の欄は、**書き先が用意できるか**で見る。sqlite3 は
                # まだ無くてよい(初回の反映で作られる)ので、フォルダを見る
                view.exists = resolved.parent.is_dir()
            else:
                view.exists = resolved.is_dir()
        except OSError:                              # 共有に届かない
            view.exists = False
        out.append(view)
    return out


def output_dir_view() -> dict:
    """**書き出したものがどこへ行くか**を、押す前に見せるための1行。

    「CSVってどこに出力してます？」と聞かれたのがこの欄のもとです。
    出したあとに出る知らせにはパスが入っていましたが、**知らせは消える**
    ので、押す前には分からないままでした。押す人が知りたいのは
    「いま押したら、どこに出るのか」なので、ボタンの隣に常に置きます。

    設定していなければ既定(この端末の作業用フォルダ)を出します ──
    「既定です」ではなく**実際のパスそのもの**を出さないと、探しに
    行けません。`is_default` は、そこが共有ではなく手元であることを
    画面が言い添えるためだけに付けています。
    """
    stored = user_settings.load_all().get(config.KEY_REPORT_OUT_DIR)
    text = stored.strip() if isinstance(stored, str) else ""
    path = SETTINGS.report_output_dir
    try:
        exists = path.is_dir()
    except OSError:                                  # 共有に届かない
        exists = False
    second = SETTINGS.report_output_dir_2
    return {
        "path": str(path),
        "value": text,
        "is_default": not text,
        # 書き出すときに作るので、**無いこと自体は困りごとではない**
        "exists": exists,
        # 集計CSVの2つ目の出力先。**押す前に、2か所に出ることを見せる**
        "second": str(second) if second is not None and second != path else "",
        "settings_url": "/settings?tab=paths",
    }


def standard_time_output_view() -> dict:
    """標準作業時間のCSVの行き先。**その画面の上で変えられる**ように値も渡す。

    空なら集計CSVと同じ先です(`config.SETTINGS.standard_time_output_dir`)。
    そのときは「集計CSVと同じ」と言い添えます ── 同じ先だと知らずに
    集計CSVの先を変えると、こちらの出る先まで動くので。
    """
    stored = user_settings.load_all().get(config.KEY_STANDARD_TIME_OUT_DIR)
    text = stored.strip() if isinstance(stored, str) else ""
    path = SETTINGS.standard_time_output_dir
    base = output_dir_view()
    return {
        "key": config.KEY_STANDARD_TIME_OUT_DIR,
        "label": PROTECTED_LABELS[config.KEY_STANDARD_TIME_OUT_DIR],
        "path": str(path),
        "value": text,
        "same_as_report": not text,
        "report_path": base["path"],
        # 集計CSVの先が既定(この端末の中)で、こちらもそれに従っているとき
        "is_default": not text and base["is_default"],
        "settings_url": "/settings?tab=paths",
    }


def _resolved_dir(key: str) -> Path:
    return {
        config.KEY_ACCESS_DIR: lambda: SETTINGS.access_dir,
        config.KEY_REFERENCE_DIR: lambda: SETTINGS.gw_reference_dir,
        config.KEY_WIP_DIR: lambda: SETTINGS.wip_master_dir,
        config.KEY_MATERIAL_DIR: lambda: SETTINGS.material_master_dir,
        config.KEY_TRANSMISSION_DIR: lambda: SETTINGS.transmission_master_dir,
        config.KEY_SOUND_DIR: lambda: SETTINGS.sound_dir,
        config.KEY_MONTHLY_DIR: lambda: SETTINGS.monthly_dir,
        config.KEY_REPORT_OUT_DIR: lambda: SETTINGS.report_output_dir,
        config.KEY_REPORT_OUT_DIR2: lambda: SETTINGS.report_output_dir_2 or Path(""),
        config.KEY_STANDARD_TIME_OUT_DIR: lambda: SETTINGS.standard_time_output_dir,
        config.KEY_VC_MASTER_DIR: lambda: SETTINGS.vc_master_dir,
        config.KEY_LOG_DIR: lambda: SETTINGS.log_dir,
        # **LocalBackup まで**出す(実際に書く先)
        config.KEY_BACKUP_DIR: lambda: SETTINGS.local_backup_root,
        # これだけはフォルダではなくファイル。**実際に書く先**を出す
        config.KEY_ACCESS_DB_FILE: lambda: SETTINGS.access_db_path,
        config.KEY_LINE_TARGET_FILE: lambda: SETTINGS.line_target_path,
        config.KEY_STOP_REASON_FILE: lambda: SETTINGS.stop_reason_csv_path,
    }[key]()


# フォルダではない設定。`resolve_dir` を通さず、「あるか」も別に見る。
# 音のファイル名もここに入る(`SOUND_FILE_KEYS`)
def _sound_file_keys() -> tuple[str, ...]:
    from ..logic.sound import SOUND_KEYS

    return tuple(config.sound_file_key(k) for k in SOUND_KEYS)


SOUND_FILE_KEYS = _sound_file_keys()
FILE_KEYS = ((config.KEY_ACCESS_DB_FILE, config.KEY_LINE_TARGET_FILE,
              config.KEY_STOP_REASON_FILE)
             + SOUND_FILE_KEYS)

# ファイルの欄のうち、**フォルダを書いてよいもの。**
#
# 日報データと音は「その置き場所の中の名前」なので、区切りが入っていたら
# 打ち間違いとして断ります。目標CSVだけは違って、共有の別のところに
# 1つ置いて全端末から読ませたい ── フォルダ付きもフルパスも通します
# (どこを見ることになるかは `config.SETTINGS.line_target_path`)
PATH_ALLOWED_FILE_KEYS = (config.KEY_LINE_TARGET_FILE,
                          config.KEY_STOP_REASON_FILE)

# **無くても困りごとにしない欄。**
#
# 目標CSVは、置いた人のところだけ目標線が出る仕組みです。使っていない
# ラインでは最初から無く、そこで赤い印を出すと「いつも何か壊れている
# 画面」になって、本当の困りごと(マスタが読めない等)が埋もれます
OPTIONAL_PATH_KEYS = (config.KEY_LINE_TARGET_FILE,
                      # 停止内訳のCSVも同じ。無ければ伝送用ファイルの表を読む
                      config.KEY_STOP_REASON_FILE,
                      # 空なら参照用マスタのフォルダ(そちらで困りごとを言う)。
                      # 無いフォルダを指していれば VC長さ計算の画面が言う
                      config.KEY_VC_MASTER_DIR,
                      # 出力先は書き出すときに作られる。無いのは普通
                      config.KEY_REPORT_OUT_DIR, config.KEY_REPORT_OUT_DIR2,
                      config.KEY_STANDARD_TIME_OUT_DIR,
                      # ログも書くときに作る(書けなければ手元へ書く)
                      config.KEY_LOG_DIR,
                      # LocalBackup は写すときに作る(親が無ければ待つ)
                      config.KEY_BACKUP_DIR)

# **空なら使わない欄。** 既定の道が無い(空 = 出さない)ので、空のときは
# 道も「ありません」も出さず「使っていません」と言う
OFF_WHEN_EMPTY_KEYS = (config.KEY_REPORT_OUT_DIR2,)

# 空のときの意味が「既定を使う」ではない欄。**何と同じになるのか**を言う
EMPTY_TEXT: dict[str, str] = {
    config.KEY_VC_MASTER_DIR: "空なら参照用マスタと同じフォルダ",
    config.KEY_REPORT_OUT_DIR2: "空なら出しません",
    config.KEY_STANDARD_TIME_OUT_DIR: "空なら集計CSVと同じ先",
    config.KEY_LOG_DIR: "空ならこの端末の logs",
    config.KEY_BACKUP_DIR: "空なら共有の日報管理のパスと同じフォルダ",
}

# そのファイルを**読むだけ**の欄。あるかどうかはファイル自身で見る
READ_FILE_KEYS = (config.KEY_LINE_TARGET_FILE, config.KEY_STOP_REASON_FILE)


def file_views() -> list[FileView]:
    """参照するファイルの1つずつの状態(マスタ管理の面)。

    **共有フォルダを開かない。** sqlite3 は `source_db` が手元へ写して
    から読むので、ここで一覧を出しても他の人の邪魔をしない。
    Access は開かずに、あるかどうかと大きさだけを見る ── 開くには
    Windows と ODBC ドライバが要り、失敗が画面を止める理由にならない。
    """
    out: list[FileView] = []
    for group in EXPECTED_FILES.values():
        for key, label, getter in group:
            out.append(_file_view(key, label, getter))
    return out


def _file_view(key: str, label: str, getter) -> FileView:
    source_key = FILE_SOURCE_KEY.get(key, "")
    source_label = {k: l for k, l, _ in PATH_FIELDS}.get(source_key, "")
    uses = [{"table": t, "purpose": p} for t, p in FILE_USES.get(key, ())]
    try:
        path = Path(getter())
    except Exception as exc:                         # noqa: BLE001 - 画面を止めない
        return FileView(key=key, label=label, error=str(exc),
                        source_key=source_key, source_label=source_label,
                        uses=uses)

    view = FileView(key=key, label=label, path=str(path), name=path.name,
                    source_key=source_key, source_label=source_label,
                    directory=str(path.parent), uses=uses)
    suffix = path.suffix.lower()
    view.kind = "sqlite3" if suffix in source_db.SUFFIXES else "access"
    view.is_write_target = key == WRITE_TARGET_KEY
    try:
        view.exists = path.exists()
        if view.exists:
            view.size = path.stat().st_size
    except OSError as exc:
        view.error = str(exc)
        return view

    if view.is_write_target:
        # 書き込み先は**まだ無くてもよい**。sqlite3 なら反映のときに
        # ファイルごと作られる(`sqlite_backend.ensure_tables`)
        view.writable = view.kind == "sqlite3"
        if not view.exists:
            view.error = ("まだありません(初回の保存で作られます)"
                          if view.writable else "ファイルがありません")
            return view
        if view.kind != "sqlite3":
            view.readable = True
            return view
        # **写しは作らない。** 自分が書く先なので、写すと書いたものが見えない
        try:
            view.tables = sorted(
                t for t in _sqlite_tables(path) if not t.startswith("sqlite_"))
            view.readable = True
        except Exception as exc:                     # noqa: BLE001 - 画面を止めない
            view.error = str(exc)
        return view

    if not view.exists:
        view.error = "ファイルがありません"
        return view

    if view.kind != "sqlite3":
        # Access は開かない。**あることが分かれば十分** ── 実際に読めるかは
        # 「③反映」「時間マスタの取り込み」を押したときに分かる
        view.readable = True
        return view

    probe = source_db.probe(path)
    view.readable = probe.ok
    view.copied_to = probe.copied_to
    view.copied_at = probe.copied_at
    view.tables = probe.tables
    view.error = probe.error
    return view


def _sqlite_tables(path: Path) -> list[str]:
    """そのファイルの表の一覧。**開くだけで写さない。**"""
    import sqlite3

    conn = sqlite3.connect(str(path), timeout=5)
    try:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    finally:
        conn.close()


# ==================================================================
# 保存
# ==================================================================
@dataclass
class SaveResult:
    ok: bool = True
    message: str = ""
    reason: str = ""
    # 断ったときに「何を変えようとしたのか」。画面がそのまま出せる形で
    changing: list[str] = field(default_factory=list)


def _protected_changes(values: dict[str, Optional[str]]) -> list[str]:
    """今回**本当に変わる**参照パスの表示名。

    値が変わらない保存で聞かないのは、設定画面が参照パスを毎回まとめて
    送るためです。音の置き場所を直しただけでパスワードを聞かれると、
    現場は「何をしても聞かれる」と受け取り、パスワードそのものが
    形骸化します。
    """
    stored = user_settings.load_all()
    changing = []
    for key, label in PROTECTED_LABELS.items():
        sent = values.get(key)
        if sent is None:
            continue
        now = stored.get(key)
        now_text = now.strip() if isinstance(now, str) else ""
        if sent.strip() != now_text:
            changing.append(label)
    return changing


# 書き先として受け付ける拡張子。**ここに無いものは断る** ── 打ち間違いを
# そのまま保存すると、反映のたびに失敗して原因が分からない
ACCESS_DB_SUFFIXES = (".accdb", ".mdb") + source_db.SUFFIXES


# 音として受け付ける拡張子。**配れるものと同じ1つ**(`logic/sound.AUDIO_SUFFIXES`)
# ── ここで通して配れないと、
# 保存できたのに鳴らない、という分かりにくい形になる
from ..logic.sound import AUDIO_SUFFIXES as SOUND_SUFFIXES  # noqa: E402


# 目標CSVとして受け付ける拡張子。**メモ帳で開けるものだけ** ── 直すのは
# 現場なので、Excel を通さないと読めない形にはしない
LINE_TARGET_SUFFIXES = (".csv", ".txt", ".tsv")


def _label_for(key: str) -> str:
    """断りの文言に出す名前。"""
    from ..logic.sound import SOUNDS

    from ..services.sound_files import texts

    for spec in SOUNDS:
        if config.sound_file_key(spec.key) == key:
            return f"「{texts(spec)[0]}」の音声ファイル名"
    return {k: l for k, l, _ in PATH_FIELDS}.get(key, key)


def _file_name_problem(text: str, suffixes: tuple[str, ...], *,
                       allow_path: bool = False) -> str:
    """ファイル名として通せるか。通せれば空文字、駄目なら理由。

    `allow_path` はフォルダ付き・フルパスも通す欄のため
    (`PATH_ALLOWED_FILE_KEYS`)。**拡張子は必ず見ます** ── 拡張子が違えば
    黙って読めないだけになり、打ち間違いに気づけない。
    """
    name = text.strip().strip('"')
    if not allow_path and ("/" in name or "\\" in name):
        return "はファイル名だけを入れてください(フォルダは上の欄です)"
    if Path(name.replace("\\", "/")).suffix.lower() not in suffixes:
        return "の拡張子は " + " / ".join(suffixes) + " のどれかにしてください"
    return ""


def _sound_off() -> str:
    from ..logic.sound import SOUND_OFF

    return SOUND_OFF


def _suffixes_for(key: str) -> tuple[str, ...]:
    """その欄で受け付ける拡張子。**振り分けの出どころはここ1つ。**"""
    if key in SOUND_FILE_KEYS:
        return SOUND_SUFFIXES
    if key in (config.KEY_LINE_TARGET_FILE, config.KEY_STOP_REASON_FILE):
        return LINE_TARGET_SUFFIXES
    return ACCESS_DB_SUFFIXES


def _with_fixed_names(values: dict[str, Optional[str]]
                      ) -> dict[str, Optional[str]]:
    """フォルダで届いた欄に、決まった名前を足す(`FIXED_NAME_KEYS`)。

    空(既定に戻す)と `None`(触らない)はそのまま。

    **足すのは「拡張子が付いていないとき」だけ**です。`目標.xlsx` と
    打たれたものに足すと `目標.xlsx\ライン毎目標.csv` になり、
    **打ち間違いがフォルダ名として通ってしまいます** ── 拡張子が付いて
    いるものはファイル名のつもりなので、そのまま下の拡張子の判定へ
    渡して、違っていれば断らせます。
    """
    out = dict(values)
    for key, name in FIXED_NAME_KEYS.items():
        text = out.get(key)
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text or Path(text).suffix:
            continue
        out[key] = str(Path(text) / name)
    return out


def _value_problem(values: dict[str, Optional[str]]) -> Optional[SaveResult]:
    """書ける形か。**断るならその理由、書けるなら None。**

    保存するすべての道で同じ決まりにするための1か所です ── 配布設定は
    この端末の値をそのまま書き出すので、ここを通った値だけが配られます。
    """
    labels = {k: l for k, l, _ in PATH_FIELDS}
    for key, text in values.items():
        if text is None or not text.strip():
            continue                                  # 空 = 既定に戻す
        if key in SOUND_FILE_KEYS and text.strip() == _sound_off():
            continue                                  # 「鳴らさない」
        if key in FILE_KEYS:
            problem = _file_name_problem(
                text, _suffixes_for(key),
                allow_path=key in PATH_ALLOWED_FILE_KEYS)
            if problem:
                return SaveResult(False,
                                  f"{_label_for(key)}{problem}",
                                  REFUSE_BAD_INPUT)
            continue
        try:
            config.resolve_dir(text)
        except ValueError as exc:
            return SaveResult(False,
                              f"{labels.get(key, key)}の書き方が正しくありません: {exc}",
                              REFUSE_BAD_INPUT)
    return None


def save(values: dict[str, Optional[str]],
         password: Optional[str] = None, *, admin: bool = False) -> SaveResult:
    """参照パスを保存する。**渡されたものだけ**を触る。

    `None` は「この項目は今回いじらない」。画面が一部だけ送ってきたときに、
    送っていない項目を消さないため。空文字は「既定に戻す」で、そのまま
    保存してよい。

    参照パスを**変えるとき**だけ管理者パスワードが要ります
    (`PROTECTED_LABELS` の説明を参照)。関門をここに置くのは、画面からも
    他の道からも同じところを通すためです。

    **すでに管理者モードなら、もう一度は聞きません**(`admin`)。
    管理者モードに入る合言葉はこれと同じもので、同じ端末・同じ直の
    あいだだけ開いています ── 面の上で鍵を開けたのに、その面のボタンが
    「パスワードが要ります」と断ると、押した人には**効いていない**と
    しか見えません。直が変われば管理者モードは自動で閉じます。
    """
    allowed = set(config.PATH_KEYS) | set(SOUND_FILE_KEYS)
    unknown = [k for k in values if k not in allowed]
    if unknown:
        return SaveResult(False, f"知らない項目です: {', '.join(unknown)}",
                          REFUSE_BAD_INPUT)

    # **フォルダを渡されたら、名前はこちらで足す。**
    # 画面はフォルダしか出していないので(`FIXED_NAME_KEYS`)、届くのも
    # フォルダです。すでに `.csv` で終わっているものは触りません ──
    # 設定ファイルに手で書いた人や、API を直接叩く人の書き方を壊さない
    values = _with_fixed_names(values)

    # 書ける形かを先に見る。**書いてしまってから「読めない道でした」は遅い**
    refused = _value_problem(values)
    if refused is not None:
        return refused

    # **書く前に通す関門。** ここより下で1つでも書いてしまうと、
    # 断ったのに一部だけ変わった状態が残る
    changing = _protected_changes(values)
    if changing and not admin:
        if not admin_password.verify(str(password or "")):
            # 合っていないのか、そもそも送っていないのかは言い分けない
            # ── 総当たりの手がかりになる。**何が要るか**だけを言う
            log.warning("参照パスの変更を断りました(管理者パスワード): %s",
                        " / ".join(changing))
            return SaveResult(
                False,
                f"{' と '.join(changing)}を変えるには管理者パスワードが要ります。",
                REFUSE_NEED_PASSWORD, changing)
        log.info("参照パスを変えます(管理者パスワード確認済み): %s",
                 " / ".join(changing))

    to_write = {k: (v or "").strip() for k, v in values.items() if v is not None}
    log_before = str(user_settings.load_all().get(config.KEY_LOG_DIR) or "").strip()
    if not user_settings.save_many(to_write):
        return SaveResult(False, "設定ファイルに書けませんでした。",
                          REFUSE_BAD_INPUT)
    user_settings.log_saved(list(to_write))

    # 置き場所が変わったので、手元の写しは捨てる。**捨てないと、
    # 変える前のフォルダから写したものを読み続ける**
    source_db.forget()
    # VC計算マスタも。読めなかった直後は30秒読みに行かないので、その
    # 待ちを解く ── 置き場所を直したのに、しばらく古い答えのままになる
    from ..vc import masters as vc_masters
    vc_masters.invalidate()
    if config.KEY_LOG_DIR in to_write and to_write[config.KEY_LOG_DIR] != log_before:
        # **ログの書き先はその場で移す**(起動し直さなくても効く)。移せなければ
        # 手元へ書いていることを言う ── 黙っていると、設定した先に記録が無い
        from ..logging_setup import redirect_logging
        moved = redirect_logging()
        if moved["problem"]:
            return SaveResult(True, "設定を保存しました。ただし" + moved["problem"]
                              + " ── この端末の logs へ書いています")
        return SaveResult(True, f"設定を保存しました。ログは {Path(moved['path']).parent} へ書きます")
    return SaveResult(True, "設定を保存しました")


# ==================================================================
# 画面へ渡す形
# ==================================================================
def line_rename_view() -> dict[str, Any]:
    """コンバートのカード(v4.13.0)。**共有のファイルは読まない**(押したときだけ読む)。"""
    from ..logic import line_names
    from ..logic import line_rename

    return {"renamed": [{"old": d.old, "official": d.official} for d in line_names.renamed()],
            "same_name": [d.official for d in line_names.DEFINITIONS if not d.renamed],
            "kinds": list(line_rename.KINDS),
            # **どのファイルか**(v4.16.0)。「共有へ保存」と同じ共有の日報データ ──
            # 置き場所は参照設定の「共有の日報管理のパス」で決まる(別に指定はしない)
            "path": str(SETTINGS.access_db_path),
            "path_label": PROTECTED_LABELS[config.KEY_ACCESS_DIR]}


def to_dict() -> dict[str, Any]:
    """設定画面ぜんぶ。**毎回まるごと返す**(部分更新の食い違いを作らない)。"""
    paths = path_views()
    files = file_views()
    folder = sound_folder_view()
    return {
        "paths": [vars(p) for p in paths],
        # 面ごとに欄を置くので、鍵で引ける形も渡す。**出どころは同じ1つ**
        "path_map": {p.key: vars(p) for p in paths},
        "files": [vars(f) for f in files],
        "sound_folder": folder,
        "sounds": sound_views(folder["files"]),
        "admin_custom": admin_password.is_custom(),
        # 変えてあるのは**この端末**か、**配布用の既定**か
        "admin_origin": user_settings.origin(config.KEY_ADMIN_PASSWORD),
        "distribution": distribution_view(),
        "admin_min_length": admin_password.MIN_LENGTH,
        "cache_dir": str(source_db.copy_dir()),
        "line_targets": line_target_view(),
        "stop_reasons": stop_reason_view(),
        # **この端末だけに残すファイル**と、**複数のPCで共有するファイル**
        "storage": _storage_view(),
        "line": line_view(),
        "line_rename": line_rename_view(),
        "problems": _problems(paths, files),
        "tabs": [vars(t) for t in TABS],
        "default_tab": DEFAULT_TAB,
        "tab_badges": tab_badges(paths, files),
    }


# ==================================================================
# 面ごとの状態
# ==================================================================
def tab_badges(paths: list[PathView], files: list[FileView],
               ) -> dict[str, dict[str, str]]:
    """面の見出しに添える印。**中身の問題を、開く前に見せる。**

    タブは中身を隠します。隠したせいで気づけなくなるなら、縦に積んで
    スクロールさせるほうがまだましです ── だから「見つかりません」が
    ある面は、開いていなくても見出しがそう言います。

    数だけでは色に頼ることになるので、文言も一緒に出します。
    """
    counted: dict[str, int] = {t.key: 0 for t in TABS}
    for item in _problem_items(paths, files):
        if item[0] in counted:
            counted[item[0]] += 1
    return {key: {"level": "ng", "text": f"{n}件"}
            for key, n in counted.items() if n}


def tab_views(*, admin: bool) -> list[dict[str, Any]]:
    """面の並びと、鍵の状態。

    鍵は**管理者モードそのもの**です。このツールに合言葉は1つしか
    なく、2つ目を作ると現場は両方を紙に貼ります。
    """
    return [{**vars(t), "locked": bool(t.lock) and not admin,
             "needs_lock": bool(t.lock)} for t in TABS]


# ==================================================================
# この端末のライン
# ==================================================================
def line_view() -> dict[str, Any]:
    """据え付けのときに決めるもの。**日報入力からは触れません。**

    入力画面の1番目に並んでいると「まず選ぶもの」に見えて、押し間違えた
    まま打ち始められます ── ラインが違えば保存先のキーごと変わるので、
    気づくのは翌日の集計です。毎直やることではないので、設定へ移して
    管理者モードの内側に置きました。
    """
    from .. import config, constants, user_settings, work_context

    ctx = work_context.get_context()
    # **出すのは端末のライン。** 過去データで他ラインの紙を開いている
    # あいだ `ctx.line` はそちらになりますが、この欄が言うのは「この端末は
    # どのラインか」です
    from ..logic import line_names

    line = ctx.terminal_line
    sub = ctx.terminal_maru_sub
    remembered = bool(user_settings.get(config.KEY_TERMINAL_LINE))
    # **どこで決まったか**(v4.12.4)。表から当てたライン(と設備番号)がいまのものと
    # 同じなら表、違えば管理者モードで選んだもの(表の値が変わるまでそのまま)
    applied = line_names.upgrade_key(user_settings.get(config.KEY_ACCESS_LINE_APPLIED) or "")
    key = f"{line}:{sub}" if sub else line
    origin = ("" if not remembered else "access" if applied == key else "manual")
    return {
        "current": line,
        # 中板は設備番号を続けて「中板4」(v4.12.4)
        "current_official": line_names.label(line, sub),
        # 並びは定義の表の順(L-1・LVC・HVC・機側・NS1・AIM・トット・バランサー・中板)
        "choices": [{"code": d.code, "official": d.official} for d in line_names.DEFINITIONS],
        "maru_sub": sub,
        "maru_choices": list(line_names.NUMBERS),
        "maru_official": work_context.MARU_LINE,
        # 中板(丸徳)だけ設備番号を持つ(`Line_AutoSelection` の A1〜A7)
        "is_maru": line == work_context.MARU_LINE,
        # 覚えているか。**覚えていなければ日報は打てない**(v4.3.0)ので、
        # 据え付けがまだ済んでいないことを画面で分かるようにする
        "remembered": remembered,
        "origin": origin,
        # マスタの「アクセス権限」の表から決めたこと(v4.12.0)
        "access": access_view(),
    }


def access_view() -> dict[str, Any]:
    """アクセス権限の表から読んだこと(`services/access_rights`)。"""
    from .. import config, user_settings
    from ..services import access_rights

    from ..logic import line_names

    view = access_rights.current().as_dict()
    view["last_applied"] = str(user_settings.get(config.KEY_ACCESS_LINE_APPLIED) or "")
    # マスタに書く**正規の呼び名**の表(v4.12.2)
    view["line_names"] = line_names.table_rows()
    return view


# ==================================================================
# 配布設定 (`distribution` / 起動用の Start.vbs と同じフォルダの `配布設定\`)
# ==================================================================
# 端末の設定は利用者ごとのローカル領域に入るので、**アプリのフォルダを
# コピーしても付いて行きません。** 1台で設定を済ませて配布用に書き出し、
# そのフォルダを配ると、どの端末も同じ設定で始まります。
def setting_label(key: str) -> str:
    """設定の鍵を、画面の欄の名前にする。**名前は欄と同じものを使う。**"""
    for field_key, label, _note in PATH_FIELDS:
        if field_key == key:
            return label
    if key == config.KEY_ADMIN_PASSWORD:
        return "管理者パスワード(撹拌して持ちます)"
    from ..logic.sound import SOUNDS

    from ..services.sound_files import texts

    for spec in SOUNDS:
        if config.sound_file_key(spec.key) == key:
            return f"音: {texts(spec)[0]}"
    return key


#: 札の重さ。**止まるもの**(alert)・**端末によって違うもの**(warn)・
#: **知っておけばよいもの**(info)
LEVEL_ALERT, LEVEL_WARN, LEVEL_INFO = "alert", "warn", "info"

#: ドライブ文字。C: 以外は割り当て(ネットワークドライブ)のことが多い
_DRIVE = __import__("re").compile(r"^([A-Za-z]):[\\/]")


def distribution_warnings(values: dict[str, Any]) -> list[dict[str, str]]:
    """このまま配ると困ること。**配る前に言う。**"""
    out: list[dict[str, str]] = []

    def add(level: str, text: str) -> None:
        out.append({"level": level, "text": text})

    if not user_settings._is_set(values.get(config.KEY_ACCESS_DIR)):
        add(LEVEL_ALERT,
            "「共有の日報管理のパス」が決まっていません ── このまま配ると、"
            "共有へ保存が各端末の中(利用者フォルダの下)へ書かれ、"
            "共有には何も届きません。")
    if not user_settings._is_set(values.get(config.KEY_REFERENCE_DIR)):
        add(LEVEL_ALERT,
            "「参照用マスタの参照パス」が決まっていません ── 各端末で"
            "GW計算・停止理由・作業者・直の時刻が読めません。")
    home = Path.home()
    for key, _label, _note in PATH_FIELDS:
        text = values.get(key)
        if not user_settings._is_set(text) or not isinstance(text, str):
            continue
        label = setting_label(key)
        try:
            resolved = config.resolve_dir(text)
        except ValueError:                            # pragma: no cover
            continue
        if resolved == home or home in resolved.parents:
            # **ログイン名が全端末で同じ**現場では、同じ道が配る先にも
            # あります(C:\Users\<同じ名前>\…)。だから「無い」ではなく
            # 「別のフォルダ」と言う ── 同じ名前で黙って動くほうが危ない
            add(LEVEL_WARN,
                f"「{label}」がこの端末の利用者フォルダの下({resolved})を"
                "指しています ── 配る先では、その端末の中の別のフォルダに"
                "なります(ログイン名が同じでも、中身は端末ごとに別で"
                "共有されません)。")
            continue
        drive = _DRIVE.match(text.strip().strip('"'))
        if drive and drive.group(1).upper() != "C":
            add(LEVEL_WARN,
                f"「{label}」はドライブ文字 {drive.group(1).upper()}: で書いて"
                "あります ── 配る先の端末でも同じ文字で割り当てられている"
                "必要があります(\\\\サーバ\\共有 の形なら端末に依りません)。")
    if not user_settings._is_set(values.get(config.KEY_REPORT_OUT_DIR)):
        add(LEVEL_INFO,
            "集計CSV・印刷用HTMLは各端末の中へ出ます(出力パスが既定のまま)。")
    if not user_settings._is_set(values.get(config.KEY_ADMIN_PASSWORD)):
        add(LEVEL_INFO,
            "管理者パスワードは既定(VBA版と同じ)のままです。")
    return out


def _terminal_value_text(item: "distribution.Item") -> str:
    """書き出すと入る値(**この端末のいま**)。入らないなら空。"""
    stored = user_settings.load_all()
    values = [stored.get(k) for k in item.setting_keys
              if user_settings._is_set(stored.get(k))]
    if not values:
        return ""
    if item.key == config.KEY_ADMIN_PASSWORD:
        return "(設定済み)"
    return "・".join(str(v) for v in values)


def distribution_view() -> dict[str, Any]:
    """配布設定の面(`distribution.summary`)に、**このまま配ると困ること**と
    **書き出すと入る値**を足したもの。

    書き出すのは**この端末のいまの設定**です(python-web-tools と同じ)。
    困りごとは2つの目で見ます:
        置いてある配布設定があれば … その中身で(配った先はこれで動く)
        無ければ                   … この端末の設定で(書き出すとこうなる)
    """
    view = distribution.summary()
    for row in view["items"]:
        row["value"] = _terminal_value_text(distribution.ITEM_BY_KEY[row["key"]])
    if view["exists"]:
        looked = dict(view["settings"])
        if view.get("has_password"):
            looked[config.KEY_ADMIN_PASSWORD] = "(設定済み)"
    else:
        looked = {k: v for k, v in user_settings.load_all().items()
                  if k in distribution.SETTING_KEYS}
    view["warnings"] = distribution_warnings(looked)
    view.pop("settings", None)
    return view


# ==================================================================
# ライン毎目標(45度線)
# ==================================================================
def line_target_view() -> dict[str, Any]:
    """いま効いている目標値。**読めた値と読めなかった行を両方出す。**

    グラフに赤い線が出ない/思った高さに出ないときに、見るところを
    ここ1つにします ── どのファイルを読んだか、各ラインが何枚か、
    読み飛ばした行はどれか。ラインの並びはツールの順(`LINE_NAMES`)で、
    **目標が無いラインも行として出す** ── 「出ていない」ことが
    見えないと、設定し忘れに気づけない。
    """
    from .. import constants
    from ..logic import line_target
    from ..services import targets as targets_service

    # **2つの出どころを、別々に読んでから重ねます。** 混ぜたものだけを
    # 持つと、読めなかったのがマスタなのかCSVなのかを言い分けられません
    from_csv = targets_service.load_csv()
    from_master = targets_service.read_master()
    found = line_target.merge(from_master, from_csv,
                              base_name=line_target.FROM_MASTER,
                              top_name=line_target.FROM_CSV)

    path = Path(from_csv.source) if from_csv.source else None
    known = {name: found.of(name) for name in constants.LINE_NAMES}
    rows = [{"line": name, "target": value,
             "text": _plain_target(value) if value else "",
             # **どちらが勝ったか。** これが出ていないと「マスタを直した
             # のに変わらない」の理由が読めません
             "origin": found.origin_of(name) if value else "",
             # 片方しか無いのか、CSVがマスタを上書きしているのか
             "master_text": _plain_target(from_master.of(name)),
             "csv_text": _plain_target(from_csv.of(name))}
            for name, value in known.items()]

    # どちらかにあるのに、ツールのラインに当たらなかった名前。**捨てない**
    # ── 打ち間違いか、マスタの呼び名のまま書いたか、どちらも直せる
    used = {line_target.normalize_line(name) for name in constants.LINE_NAMES}
    unknown = [{"line": name, "target": value,
                "text": _plain_target(value),
                "origin": found.origins.get(name, "")}
               for name, value in found.values.items() if name not in used]

    exists = False
    try:
        exists = bool(path and path.is_file())
    except OSError:                                  # 共有に届かない
        exists = False

    # **ファイルごとの困りごとと、行ごとの困りごとを分ける。**
    # 混ぜると「まだ置いていない」だけで「読めない行があります」と出て、
    # 直すところが無いのに赤くなる
    whole = [p for p in from_csv.problems if p.line_no == 0]
    master_whole = [p for p in from_master.problems if p.line_no == 0]
    return {
        "source": from_csv.source,
        "setting_key": config.KEY_LINE_TARGET_FILE,
        "exists": exists,
        "rows": rows,
        "unknown": unknown,
        "count": sum(1 for v in known.values() if v),
        # 出どころごとの数。**「CSVが何行効いているか」が一目で分かる**
        "csv_count": sum(1 for r in rows if r["origin"] == line_target.FROM_CSV),
        "master_count": sum(1 for r in rows
                            if r["origin"] == line_target.FROM_MASTER),
        # 置いていないことは、下の印で言う。ここは**読めなかったとき**だけ
        "error": whole[0].reason if (whole and exists) else "",
        # マスタが読めないこともある。**CSVの話と混ぜない**
        "master_error": master_whole[0].reason if master_whole else "",
        "problems": [p.as_dict() for p in from_csv.problems if p.line_no],
        # マスタの行で読まなかったもの(正規でない呼び名。v4.12.3)
        "master_problems": [p.as_dict() for p in from_master.problems if p.line_no],
        "master": str(SETTINGS.gw_material_master_path),
        "master_table": SETTINGS.line_target_table,
    }


def _storage_view() -> dict[str, Any]:
    """ファイルの置き場所(`presenters/storage`)。**読めなくても画面は出す。**"""
    from . import storage
    try:
        return storage.storage_view()
    except Exception as exc:                          # noqa: BLE001 - 画面を止めない
        log.exception("ファイルの置き場所を組めませんでした")
        return {"local_root": "", "local": [], "shared": [], "error": str(exc)}


# ==================================================================
# 停止内訳(停止内訳.csv / 伝送用ファイル 作業停止時間内訳_1〜3)
# ==================================================================
def stop_reason_view() -> dict[str, Any]:
    """いま使っている停止内訳。**分類ごとに、CSVとマスタのどちらを使っているか。**

    CSVが勝つので、「表を直したのに一覧が変わらない」(CSVに同じ分類の
    行がある)が起こり得ます ── ライン毎目標と同じく、出どころを並べて
    見せます。表とCSVは**両方読みます**(比べて見せるため)。
    """
    from ..access_bridge import stop_master
    from ..logic import stop_csv

    try:
        src = stop_master.load_sources(read_master=True)
    except Exception as exc:                          # noqa: BLE001 - 画面を止めない
        log.exception("停止内訳を読めませんでした")
        return {"source": str(SETTINGS.stop_reason_csv_path), "exists": False,
                "error": str(exc), "groups": [], "problems": [],
                "warnings": [], "count": 0, "csv_count": 0,
                "master": str(SETTINGS.stop_reason_master_path),
                "setting_key": config.KEY_STOP_REASON_FILE}

    groups = []
    for category in stop_master.CATEGORY_TABLES:
        origin = src.origins.get(category, "")
        effective = src.effective.get(category, [])
        csv_rows = src.csv.of(category)
        master_rows = src.master.get(category, [])
        groups.append({
            "category": category,
            "number": stop_csv.number_of(category),
            "call": stop_csv.call_of(category),
            "table": stop_master.CATEGORY_TABLES[category],
            "kind": stop_csv.CHAR_TYPE_WORDS.get(stop_csv.kind_of(category), ""),
            "origin": origin,
            "reasons": [{"code": r.code, "label": r.label}
                        for r in effective if r.code],
            "csv_count": len(csv_rows),
            "master_count": sum(1 for r in master_rows if r.code),
            # **表が読めなかった**。CSVで動いているなら困りごとではない
            "master_error": src.master_errors.get(category, ""),
        })
    problems = [p.as_dict() for p in src.csv.problems if p.level == stop_csv.SKIP]
    warnings = [p.as_dict() for p in src.csv.problems if p.level == stop_csv.WARN]
    return {
        "source": src.csv_path,
        "setting_key": config.KEY_STOP_REASON_FILE,
        "exists": src.csv_exists,
        "error": src.csv_error,
        "groups": groups,
        "count": sum(len(g["reasons"]) for g in groups),
        "csv_count": sum(1 for g in groups if g["origin"] == stop_csv.FROM_CSV),
        "problems": problems,
        "warnings": warnings,
        "master": str(SETTINGS.stop_reason_master_path),
    }


def _plain_target(value: Optional[float]) -> str:
    """目標を画面の文字に。**整数は整数のまま**(12.0 と出さない)。"""
    if not value:
        return ""
    return str(int(value)) if value == int(value) else str(value)


def sound_views(files: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """出来事ごとの音。**鳴らさない / 音声フォルダのファイルから選ぶ**(v4.4.0)。

    音は補助で、伝えたいことは文言のほうにあります(`logic/sound.py`)。
    ファイルが無ければ音が鳴らないだけで、催促そのものは画面に出ます。

    選択肢は「鳴らさない」+ 音声フォルダの音声ファイル。いま選んである
    ファイルや既定のファイルがフォルダに無いときも、**消さずに選択肢に
    残します**(「フォルダにありません」と添える)── 黙って別のものに
    すり替わると、保存しただけで鳴る音が変わります。
    """
    from ..logic.sound import SOUND_OFF, SOUNDS
    from ..services import sound_files

    if files is None:
        files, _problem = sound_files.folder_files()
    out = []
    for spec in SOUNDS:
        current = SETTINGS.sound_file(spec.key)          # 空 = 鳴らさない
        path = SETTINGS.sound_path(spec.key)
        names = list(files)
        for extra in (spec.default_file, current):
            if extra and extra not in names:
                names.append(extra)
        options = [{"value": SOUND_OFF, "label": "鳴らさない",
                     "selected": not current}]
        for name in names:
            notes = (["既定"] if name == spec.default_file else []) \
                + ([] if name in files else ["フォルダにありません"])
            options.append({
                "value": name,
                "label": name + (f"({'・'.join(notes)})" if notes else ""),
                "selected": name == current})
        label, when = sound_files.texts(spec)
        out.append({
            "key": spec.key, "label": label, "message": spec.message,
            "when": when,
            "setting_key": config.sound_file_key(spec.key),
            "file": current,
            "off": not current,
            "default_file": spec.default_file,
            "is_default": current == spec.default_file,
            "path": str(path) if path else "",
            "exists": sound_files.playable(spec.key),
            "options": options,
        })
    return out


def sound_folder_view() -> dict[str, Any]:
    """音声フォルダの様子(設定画面の音の面)。"""
    from ..services import sound_files

    files, problem = sound_files.folder_files()
    return {"dir": str(SETTINGS.sound_dir), "files": files, "error": problem}


def _problem_items(paths: list[PathView], files: list[FileView],
                   ) -> list[tuple[str, str]]:
    """いま困っていること。`(どの面のことか, 文言)`。

    **面の印(`tab_badges`)と、レールの印を同じ出どころにする。**
    見出しの数字と、下に並ぶ文言が食い違うと、どちらを信じればよいか
    分からなくなります。
    """
    out: list[tuple[str, str]] = []
    for view in paths:
        if view.key in OPTIONAL_PATH_KEYS:
            # 無くても動く欄。**赤い印を出さない**(理由は定義のところ)
            continue
        if not view.exists:
            # 「見つかりません」だけでは**何が**見つからないのかが
            # 読めません。フォルダなのかファイルなのかまで言います
            out.append((view.tab,
                        f"{view.label}: {view.missing_text}({view.resolved})"))
    for view in files:
        if view.is_write_target and not view.exists and view.writable:
            # 初回の反映で作られる。**無いことは困りごとではない**
            continue
        if view.key in CREATED_ON_USE and not view.exists:
            # 初めて使うときにこちらで作る(VC計算マスタ)
            continue
        tab = FILE_TAB.get(view.key, "")
        if not view.exists:
            out.append((tab, f"{view.label} がありません: {view.path}"))
        elif not view.readable:
            out.append((tab, f"{view.label} を読めません: {view.error}"))
    return out


def _problems(paths: list[PathView], files: list[FileView]) -> list[str]:
    """いま困っていること。**レールの印と同じ出どころにする。**"""
    return [text for _tab, text in _problem_items(paths, files)]
