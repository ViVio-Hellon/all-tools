# VBA 版との対応表

土台(起動・停止・画面の作法)は python-web-tools と同じです。ここでは点検表の業務処理の対応をまとめます。

移行元: [docs/vba_reference/](vba_reference/)（標準モジュール・UFPrintManager・UFPreview）

## 処理の対応

| VBA | Python 版 | 備考 |
|---|---|---|
| `GetBaseFolderPath` / `DEFAULT_Path` / `DEFAULT_FOLDER` | `config/inspection.json` の `root_folder` ＋ `SettingsService.effective_root_folder` | 既定値は同じパス。二重の `\` は正規化 |
| `SaveBaseFolderPath`（名前定義 FolderPath） | `SettingsService.update(root_folder=…)`（利用者ごとの JSON） | ブックではなくユーザー別ローカル領域へ保存 |
| `SelectFolderDialog` / `ShowFolderSettings` | 画面の「フォルダ設定」＋ `/api/fs/list`(サーバ側のフォルダ参照) | 同じ説明文・同じタイトル。パスの直接入力も可。tkinter は使わない(python-web-tools と同じ) |
| `GetWorkbooksFromFolder` | `inspection_service.scan_folder` | ルート＞カテゴリー＞サブカテゴリー＞xlsm/xlsx/xls |
| `GetUniqueCategoriesFromFolder` / `SortCollectionAscending` | 大文字小文字を区別しない昇順（vbTextCompare 相当） | |
| `CreateCheckBoxes` の接頭辞判定 | `inspection_service.display_name` | 「カテゴリ_サブカテゴリ_」で始まるものだけ表示し、接頭辞を除いて表示 |
| 遅延読み込み（`LoadCurrentTabContent`） | 一覧を一括取得し、画面で切り替え | ブラウザ描画は速いため不要 |
| `DoBulkCheck`（全選択/解除の切り替え） | 「表示中を全選択」「表示中を全解除」ボタン | 下記「変更点」参照 |
| `DoMainCategoryCheckAll` | 「カテゴリ全選択」（＋「カテゴリ全解除」を追加） | 状態表示「全選択完了」「全て選択済み」も同じ |
| `UpdateSelectedList`（`・[メイン - サブ] シート名`） | 右側の「選択中」一覧 | 番号付き・並べ替え・個別解除を追加 |
| `UpdateStatus`（lblStatus） | 帯の「選択 n件」・いま動いているもの(印刷・プレビュー・フォルダ検索)・トースト | python-web-tools と同じ帯の作法 |
| `lblFolderInfo`（検出: n / 更新: mm/dd h:nn） | 帯の「検出 n件」「更新 HH:MM」「点検表フォルダ」 | |
| `DoPrint`（部数の検証） | `print_service.parse_copies` | 数値→整数化→1～100 に丸め。数値以外は同じエラー文 |
| `PrintWorkbooksFromFolder` | `excel_worker.vbs` の `RunPrint` / `PrintOne` | 読み取り専用で開く・最初の表示シート・`PrintOut Copies` |
| `UFPreview.GeneratePreview` | `excel_worker.vbs` の `RunPreview` / `GetPreviewRange` / `CopyRangePicture` | 最初の表示シート・印刷範囲・UsedRange 40×16・A1:P40 |
| `CreatePictureFromClipboard`（EMF 優先→Bitmap） | `clipboard_image.read_image_png` | ctypes で同じ順に取得し PNG 化 |
| `ConvertR1C1ToA1` | 名前定義 `Print_Area` を直接参照し、文字列の場合は `ConvertFormula` | 下記「変更点」参照 |
| `DarkModeEnabled`（既定 True） / `ToggleDarkMode` | `settings.dark_mode`（既定 True）/ 帯の「ライト」「ダーク」ボタン | ボタンに切り替え先を表示するのも同じ。色は python-web-tools の tokens.css |
| `ShowPrintManager`（多重表示防止・進捗バー） | `launch_guard.py`（多重起動防止）・起動待機画面(段: 実行環境 → 準備 → 点検表フォルダ → 完了)・フォルダ検索中の表示 | |
| 「閉じる」ボタン | 「終了」ボタン（確認後にアプリを終了） | |
| `CreateFolderStructure` / `TestFolderStructure` | 移行対象外 | 一度きりの移行ツール。一覧画面・`start.bat` の診断表示で確認可能 |

## 意図的に変えた点

| 項目 | VBA 版 | Python 版 | 理由 |
|---|---|---|---|
| 印刷順 | チェックボックスの作成順 | **選択した順番**（並べ替え可能） | 要件定義書 14.2 |
| 全選択/解除 | 1 つのボタンで切り替え。一部だけ選択されているときは何もしない | 「全選択」「全解除」を分けた | 要件定義書 8。一部選択時に反応しない分かりにくさを解消 |
| 印刷前の確認 | なし | 件数・部数・プリンターを確認してから印刷 | 大量の誤印刷を防ぐ |
| 印刷の中止 | なし | 現在の 1 件が終わった時点で中止 | 同上 |
| 印刷範囲の取得 | `PrintArea` の文字列に "R" と "C" が含まれると R1C1 とみなす | 名前定義 `Print_Area` を直接使う | 列 C～R を含む範囲（例 `$C$1:$R$40`）で VBA 版はプレビューに失敗していたため |
| マクロ | 開いた点検表のマクロ・イベントが動く | 既定で無効（`disable_macros`） | 非表示の Excel で MsgBox 等が出ると停止するため。必要なら設定で有効化 |
| パスワード付きブック | 入力待ちになる | 「開けませんでした」と表示 | 非表示の Excel で入力待ちになると停止するため（`block_password_prompt`） |
| Excel ロックファイル（`~$…`） | 接頭辞の判定で結果的に非表示 | 明示的に除外 | 動作は同じ |
| 命名規則に合わないファイル | 表示されない（気付きにくい） | 表示しないが「表示していないファイル: n 件」と一覧を表示 | 原因の追跡のため |
| プレビュー画像 | 毎回 Excel で作成 | Excel ファイルの更新日時が同じならキャッシュを表示 | 2 回目以降を速くするため。Excel を修正すると自動で作り直す |
