# data

アプリがデータとして読み込むファイルを置く場所です(初期版では未使用)。

- 点検表 Excel はここに置かず、外部の点検表ルートフォルダ(`config/inspection.json` の `root_folder`)を参照します。
- 実行中に変化するファイル(ログ・キャッシュ・ロック・利用者設定など)は `%LOCALAPPDATA%\InspectionSheetPrint\` に保存されます。
