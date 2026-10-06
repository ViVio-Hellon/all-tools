# 点検表 選択・印刷(Python 版)

Excel VBA の「シート印刷管理」フォーム(UFPrintManager / UFPreview)を、
**Python + Rust/Tauri(デスクトップ版)** に移行したものです。点検表そのものは **Excel ファイルのまま** 使います。

VER2.0.0 から **デスクトップ版(`InspectionSheet.exe`)** を使います。専用の窓で動き、**ポートを使いません**。
ブラウザ版(`Start.vbs` / `start.bat`)も予備として残してあります。**2つは同時には動きません**
(後から開いたほうが理由を出して止まります)。詳しくは [docs/デスクトップ版.md](docs/デスクトップ版.md)。

土台(起動・多重起動防止・待機画面・心拍と自動終了・停止・画面の作法)は
**[python-web-tools](https://github.com/ViVio-Hellon/python-web-tools)(梱包資材総合ツール)と同じ**です。
同じ端末で並べて使うものなので、起動の仕方・帯・ボタンの色・終わり方をそろえています。

- 点検表フォルダ(ルート > カテゴリー > サブカテゴリー > Excel)から一覧を自動作成
- 選択・カテゴリ/表示中の全選択・全解除・**選んだ順番**の並べ替え・検索
- Excel の `CopyPicture` によるプレビュー(HTML へは変換しない)
- Excel の `PrintOut` による印刷(印刷範囲・用紙・余白などは Excel 側の設定をそのまま使用)
- 印刷部数(1～100)・進み具合(「2 / 5 点検表Bを印刷しています」)・二重実行防止・中止
- ダーク/ライト(VBA 版と同じく既定はダーク)
- 起動待機画面・多重起動防止・ログ・安全な停止・タブを閉じたら自動終了
- エラーの後追い: 断りの文に問い合わせ番号、設定の「ログ」タブでなぜなぜ(保存先は共有フォルダも可)
  (**裏に回ったタブ・PC のスリープ明けでは終わらない**)

## 動作環境

| 項目 | 内容 |
|---|---|
| OS | Windows(Excel がインストールされたラインPC) |
| Python | 3.9 以上 + `requirements.txt`(**Flask / waitress の2つだけ**。python-web-tools と同じ) |
| デスクトップ版 | `InspectionSheet.exe`(Microsoft Edge WebView2 で画面を出す。Windows 10/11 に標準で入っている)。Python は各PCのものを使う(同梱しない) |
| ブラウザ版(予備) | Microsoft Edge / Google Chrome |

```bat
pip install -r requirements.txt
```

Excel の操作は pywin32 を使わず、Windows に最初から入っている
**cscript.exe + VBScript(COM)** で行います(`app/vbscript/excel_worker.vbs`)。
プレビュー画像は ctypes(Windows API)+ 標準ライブラリで PNG にします(Pillow 不要)。

## 使い方

| ファイル | 用途 |
|---|---|
| **InspectionSheet.exe** | **デスクトップ版(ふだんはこれ)**。専用の窓で開きます。ポートを使わないので、プロキシ・セキュリティ製品・ポートの取り合いに左右されません。窓の × / 画面の「終了」で閉じます(印刷中は確認)。設定のフォルダは Windows の「フォルダーの選択」窓でも選べます。 |
| **Start.vbs** | ブラウザ版(予備)の起動(ダブルクリック)。コンソールを出さず、すぐに待機画面が開き、準備ができると点検表の画面に切り替わります。既に起動していれば、その画面を開きます。 |
| **start.bat** | 診断起動。環境確認(Python・Flask/waitress・書き込み・通信・プリンター)を進み具合の棒つきで表示してから起動します。Excel の起動はブラウザが開いたあとに確かめ、その Excel を最初のプレビュー・印刷に使います。起動しないときに使います。 |
| **stop.bat** | ブラウザ版の停止(デスクトップ版は窓で閉じるよう案内します)。まずアプリへ終了を頼み、応答しないときだけ **記録した PID(生成時刻で本人確認)** を止めます。印刷中は断ります(`stop.bat --force` で中断して停止)。 |

どの版が動いているかは、画面左上のアプリ名の右(`VER2.0.0`)で分かります。各ダイアログの右下にも出ます。

画面右上の「終了」でも終了できます。**タブを閉じると 8 秒後に自動で終了**します
(再読込ならその間に戻るので終わりません)。裏に回ったタブ・スリープ中は終了しません。

### Excel なしで画面を確認する(模擬モード)

```bat
start.bat --demo
```

見本フォルダ(`%LOCALAPPDATA%\InspectionSheetPrint\work\demo_一括管理_点検表`)を使い、
プレビューは仮の画像、印刷は模擬動作になります。

## 設定

| ファイル | 内容 |
|---|---|
| `config/app.json` | アプリの識別(`app_id`)・表示名・**版**(上げ方は [docs/変更履歴.md](docs/変更履歴.md))・ポート・監視の間隔(python-web-tools と同じ形) |
| `config/inspection.json` | 点検表ルートフォルダ・拡張子・命名規則・Excel の扱い・部数の上限(各キーの説明は `app/services/settings_service.py`) |

画面右上の「設定」には3つのタブがあります。

| タブ | 内容 |
|---|---|
| 点検表フォルダ | 点検表の親フォルダ(VBA 版のフォルダ設定) |
| 管理者パスワード | 配布設定を操作するときの確認。変えていない端末は梱包資材総合ツールと同じ既定の値 |
| 配布設定 | この端末の設定を `配布設定\` へ書き出し、ほかのラインの端末が起動時に読み込む(python-web-tools から移植) |

変えた値は利用者ごとに `%LOCALAPPDATA%\InspectionSheetPrint\data\user_settings.json` へ保存します
(設定ファイルは書き換えません。管理者パスワードは撹拌して保存します)。
複数ラインへ揃える手順は [docs/複数ライン運用.md](docs/複数ライン運用.md)。

## フォルダ構成

```text
<ApplicationRoot>
├─ InspectionSheet.exe デスクトップ版(GitHub Actions が作る。リポジトリには入れない)
├─ bridge.py           デスクトップ版の入口(標準入出力で要求を受け、Flask を呼ぶだけ。待ち受けない)
├─ src-tauri/          デスクトップ版の外枠(Rust/Tauri: 窓・Python の監視・同時起動の防止・部品を返す)
├─ Start.vbs / start.bat / stop.bat   ブラウザ版(予備)の起動・診断起動・停止(CP932 / CRLF)
├─ start_app.py        起動の開始点(環境確認 → ロック → 待機画面 → 本体 → 準備完了)
├─ launch_guard.py     多重起動防止(O_EXCL のロック・app_id の照合・版違いの入れ替え)
├─ boot_server.py      Flask より前に待機画面を出す最小の WSGI と、本体への引き継ぎ
├─ server.py           waitress の待ち受け・準備状態・停止
├─ process_manager.py  状態確認と安全な停止(stop.bat の中身)
├─ loading.html        起動待機画面の骨格(core/boot_screen.py が埋める)
├─ requirements.txt    Flask / waitress
├─ core/               共通基盤(app_config / logging_utils / idle_exit / boot_screen / process_tracking)
├─ app/                点検表システム固有
│  ├─ __init__.py      Flask の組み立て・セキュリティ・在席の合図
│  ├─ business.py      業務の入れ物(プロセスに1つ)
│  ├─ routes/          health / inspection / preview / printing / settings
│  ├─ services/        inspection / excel / preview / print / settings / fs_browse ほか
│  ├─ repositories/    利用者設定・プレビュー画像の控え
│  ├─ models/          データ構造
│  ├─ templates/       base.html / main.html
│  ├─ static/          css(tokens/base/components/layout = python-web-tools と同じ + inspection.css)・js(ES modules)
│  └─ vbscript/        excel_worker.vbs(Excel COM 操作。ASCII / CRLF)
├─ config/             app.json / inspection.json
├─ data/ assets/       (初期版では未使用)
├─ docs/               設計・VBA 対応表・テスト項目・元の VBA
└─ tests/              自動テスト(unittest)
```

実行中に変化するファイルは、アプリ本体(共有フォルダでも可)とは分けて
`%LOCALAPPDATA%\InspectionSheetPrint\` に保存します。

```text
%LOCALAPPDATA%\InspectionSheetPrint
├─ runtime   app.lock(PID・生成時刻・ポート・トークン)・tracked_processes.json(起動した Excel / cscript)
├─ logs      inspection_YYYYMMDD.log(流れ)・events_YYYYMMDD.jsonl(なぜなぜ用の出来事)
│            ※ 設定の「ログ」タブで共有フォルダなどへ変えられる(docs/ログとなぜなぜ分析.md)
├─ pycache   Python キャッシュ(共有フォルダに __pycache__ を作らない)
├─ cache     プレビュー画像(Excel の更新日時が変わると作り直し)
├─ work      Excel 処理の一時ファイル・模擬モードの見本フォルダ・起動エラー.html
├─ backup    利用者設定の変更前バックアップ
└─ data      利用者設定(点検表フォルダ・テーマ・管理者パスワード)・一覧の控え
```

## テスト

```bat
python -m unittest discover -s tests -t .
```

Windows 以外でも実行できます(Excel 部分は VBScript と同じ手順で応答する偽の worker で確認)。
実機での確認項目は [docs/test_checklist.md](docs/test_checklist.md) を参照してください。

## 資料

- [docs/複数ライン運用.md](docs/複数ライン運用.md) … 複数ラインで使うときに何を共有しているか・運用の決まり
- [docs/変更履歴.md](docs/変更履歴.md) … 版の上げ方と、版ごとに何が変わったか
- [docs/architecture.md](docs/architecture.md) … 全体構成・起動の流れ・Excel 連携の手順・自動終了の判定
- [docs/vba_migration_map.md](docs/vba_migration_map.md) … VBA の各処理と Python 版の対応、意図的に変えた点
- [docs/test_checklist.md](docs/test_checklist.md) … 要件定義書 39・40 に沿った確認項目
- [docs/vba_reference/](docs/vba_reference/) … 移行元の VBA ソース
