# 設計概要

土台は [python-web-tools](https://github.com/ViVio-Hellon/python-web-tools)(梱包資材総合ツール)と同じです。
起動・多重起動防止・待機画面・心拍と自動終了・停止・画面の作法(帯・色・ボタン)をそろえ、
点検表システム固有の部分(一覧・Excel 連携・プレビュー・印刷)だけを `app/` に置いています。

## 1. 全体構成

```text
┌────────────────────────────────────────────┐
│ ブラウザ(Edge / Chrome)                      │
│  base.html / main.html                        │
│  js: app / api / health / inspection /        │
│      preview / printing / running / dialog    │
└──────────────────────┬─────────────────────┘
                       │ HTTP(127.0.0.1 のみ・起動トークン・同一オリジン)
┌──────────────────────▼─────────────────────┐
│ Python: Flask + waitress                     │
│  start_app / launch_guard / boot_server /    │
│  server / process_manager   … 起動と停止     │
│  core/   設定・ログ・自動終了・待機画面・PID  │
│  app/    routes → services → repositories    │
└──────────────────────┬─────────────────────┘
                       │ subprocess(標準入出力 + UTF-16 ファイル)
┌──────────────────────▼─────────────────────┐
│ cscript.exe + app/vbscript/excel_worker.vbs  │
└──────────────────────┬─────────────────────┘
                       │ COM
┌──────────────────────▼─────────────────────┐
│ Excel(非表示・処理ごとに起動 → 必ず終了)   │
└────────────────────────────────────────────┘
```

## 2. 起動の流れ

```text
Start.vbs(CP932)
 ├─ start_app.py があるか / python・pythonw があるかを確かめる(無ければ MsgBox)
 └─ pythonw start_app.py(コンソールを出さない)

start_app.py
 ├─ 環境確認(Python 3.9+・Flask/waitress・ローカル領域の書き込み・設定ファイル)
 ├─ launch_guard.try_acquire()  … runtime\app.lock を O_CREAT|O_EXCL で作る(2つ目は必ず失敗)
 │    └─ 取れなければ check_existing():
 │         ・同じ app_id・同じ版が動いている → その画面を開いて終了
 │         ・版が違う → 古いほうに /api/shutdown を頼んで入れ替え
 │         ・ロックの PID が居ない/別物 → ロックを掃除して起動
 ├─ ポートを選ぶ(8733 から 3 つ先まで)
 ├─ AppServer: まず boot_server.BootApp で待ち受け(Flask を読む前。約 0.1 秒で待機画面)
 ├─ ブラウザを開く(/?t=トークン)… 待機画面が /api/health を見て段を進める
 ├─ build(): Flask 本体を組み立て、**待ち受けを開き直さずに**引き継ぐ(Handover)
 ├─ 業務の初期化: 前回残った Excel の片付け・古いプレビュー画像の削除・フォルダ検索開始
 ├─ 自動終了の見張り(core/idle_exit)を立てる
 └─ 前回の一覧の控えがあれば、すぐ ready=true(確認は裏で続き、終われば一覧が替わる)。
    控えが無ければ、フォルダ検索が終わったら ready=true
```

起動に失敗したときは、`%LOCALAPPDATA%\InspectionSheetPrint\work\起動エラー.html` を開いて
理由と `start.bat` での確認方法を案内します(pythonw はコンソールを持たないため)。

## 3. 画面(python-web-tools の UIUX 設計指針に合わせる)

- **帯**: アプリ名・版(VER1.0.0)・選択件数・検出件数・更新時刻・点検表フォルダ・
  いま動いているもの(印刷/プレビュー/フォルダ検索)・再読込・設定(点検表フォルダ / 管理者パスワード / 配布設定のタブ)・テーマ・終了
- **結果:操作 = 左:右**。左に カテゴリー(タブ)> サブカテゴリー > 点検表、右に 選択中(選んだ順)・部数・プレビュー・印刷
- 色は `tokens.css` からだけ取る。選ばれているものは**構造の色(ティール)**、押すものは**操作の色**
  (プレビュー=実行の青、印刷=印刷の赤橙、終了・中止=危険)
- 押せるものは 44px 以上。ページ全体はスクロールさせず、一覧と選択中の中だけが動く
- 確認のモーダルは取り消せない操作(印刷・終了)だけ。それ以外はトースト
- 押したボタンは 250ms を過ぎると待機の姿になる(`busy.js`)

## 4. Excel 連携(excel_worker.vbs)

Python と VBScript は、標準入出力で 1 行ずつコマンドをやり取りします。
**ファイルは一切受け渡しません。** Microsoft Store 版の Python は %LOCALAPPDATA% / %TEMP%
への書き込みを自分専用の控えへ振り替えるため、Python が書いたファイルが cscript / Excel
からは「パスが見つかりません」になるからです(VER1.1.0 まではファイルを使っていて、
ラインPCで実際に起きました)。

- 指示: Python → 標準入力 `SET <key> <hex>` … `ENDJOB`
- 結果: 標準出力 `RESULT <key> <hex>`(`item.1.code` など)
- `<hex>` は UTF-16 の1単位を4桁の16進にしたもの。日本語のパスやエラー内容も
  ASCII のまま送れる(文字化けしない)

### プレビュー

```text
Python                                   VBScript / Excel
  ── SET file.1 <hex> … ENDJOB ────▶
                                          READY preview
                                          EXCEL <hwnd>     ← Python が Excel の PID を記録
                                          (読み取り専用で開く・最初の表示シート・印刷範囲)
                                          (Range.CopyPicture xlScreen, xlPicture)
                                          COPIED 1
  クリップボードの EMF を読み PNG 化
  (拡大率 2 倍で描画・Excel が起動中のうちに読む)
  ── OK ──────────────────────────────▶  Close → Quit
                                          FINISH 0
  Excel の PID が終了したことを確認(残っていればその PID だけ強制終了)
```

クリップボードから読めなかった場合は `EXPORT` を返し、VBScript が一時ブックのグラフに
貼り付けて自分の TEMP へ PNG を書き出し、`PNGDATA <base64>` で送ります
(点検表ブック自体は変更しません)。

Excel は同時に1つの処理しかしないので、プレビューは順番待ちをします
(`ExcelService.acquire`)。タブを何枚開いても Excel とサーバの手をふさがないように:

- 同じ画面(`X-Screen-Id`)から次のプレビューが来たら、前の要求は順番待ちをやめて
  `409 SUPERSEDED` を返す(画面はもう次の点検表を見ているので何も出さない)
- 順番待ちは `MAX_WAITING`(4)件まで。それ以上は `409 PREVIEW_QUEUE_FULL`
  (サーバの手は 8 つ。進み具合・接続確認に答える手を残す)
- 画面側は ↓↑ で送っているあいだは頼まず、止まってから(0.18 秒)頼む

プレビュー範囲の決め方は VBA 版 `GeneratePreview` と同じです。

1. 印刷範囲(名前定義 `Print_Area`。複数範囲なら 1 つ目)
2. 印刷範囲の文字列(R1C1 形式なら `ConvertFormula` で A1 形式へ)
3. `UsedRange`(最大 40 行 × 16 列)、取得できなければ `A1:P40`

### 印刷

```text
                                          READY print / EXCEL <hwnd>
                                          NEXT 1
  ── GO(中止時は STOP)────────────▶
                                          BEGIN 1 → 開く → 最初の表示シート.PrintOut Copies
                                          END 1 OK|NG
                                          NEXT 2 …
```

1 回の印刷ジョブは 1 つの Excel で選択順に処理します。
各段階で一定時間(既定 180 秒)応答がない場合は、その Excel と cscript だけを終了し、
残りの点検表は「中止」として結果に表示します。

### Excel の使い回し(VER1.2.0)

Excel の起動と終了はそれぞれ数秒かかるので、**1つの cscript + Excel を続けて使います。**

- 処理(プレビュー・印刷)が終わっても、Excel は `excel.keep_alive_sec`(既定 120 秒)残す。
  次の処理は `SET … ENDJOB` を送るだけで始まる。ブックは処理ごとにすべて閉じる
- 使われなければ Python が標準入力を閉じ、VBScript が Excel を終了して抜ける(`BYE`)
- 点検表を選び始めたら(`POST /api/excel/warm`)Excel を裏で起動しておく
- 応答が無い・途中で止まった処理プログラムは使い回さず、止めて次は起動し直す
- アプリの終了・`start.bat --check`(`--check --check-excel`)のあとは必ず閉じる
- `start.bat` の起動(`--check-excel`)は、画面が出たあとで Excel を確かめ、**閉じずに残す**
  (閉じきるまで実機で十数秒かかり、すぐ次のプレビューでまた起動することになるため)
- 裏の Excel は `IgnoreRemoteRequests = True`(利用者がダブルクリックしたファイルを
  受け取らない)。終了前に `False` へ戻す(Excel はこの設定を保存するため)

### Excel プロセスの後始末

- `Application.Hwnd` から PID を求め、**PID + プロセス生成時刻** を `tracked_processes.json` に記録
- Excel を閉じるとき、終了しなければ猶予(15 秒)後にその PID だけ強制終了
- アプリ起動時・終了時・stop.bat 実行時にも記録を確認し、残っていれば片付ける
- 利用者が自分で開いている Excel は記録されていないため、決して終了させない

## 5. 心拍と自動終了(core/idle_exit.py = python-web-tools と同じ)

窓の無いアプリなので、タブを閉じたら終わる。ただし**使っている最中には終わらせない**。

| 状況 | ブラウザ(health.js) | サーバー(IdleWatch) |
|---|---|---|
| 表示中 | 20 秒ごとに `POST /api/alive`、15 秒ごとに `/api/health` | 画面ごとに最後の心拍を覚える。90 秒途絶で終了候補 |
| 裏に回った / 固まった | `visibilitychange` / `freeze` で `visible:false` を `sendBeacon` | その画面は**心拍が途切れても落とさない** |
| 表に戻った | `resumed` を送り、すぐ `/api/health` で確かめる | 表の画面として見直す |
| PC スリープ明け | 心拍の間隔が大きく空いたら `resumed` + `gap_ms` | 見張りの壁時計の飛びを検知し、90 秒待ち直す |
| タブを閉じた | `pagehide` で `leaving` を `sendBeacon` | 8 秒の猶予(再読込なら戻って取り消し)のあと、他に画面が無ければ終了 |
| 印刷中 | — | 誰も見ていなくても**終わらせない**(終わってから判断) |
| 接続が切れた | 2 回続けて届かなければ赤い帯「バックエンドに接続できません[再接続][再読込]」 | — |
| サーバーが入れ替わった | `/api/health` の pid が変わったら読み込み直す | — |

画面からの要求(`X-Screen-Id` 付き)も在席の合図として数えます。
画面番号の無い `/api/health`(2 回目の起動の判定・`stop.bat --status` など)は数えません。

## 6. 停止(process_manager.py = stop.bat)

1. `app.lock` のポートへ `/api/health` → 自分のアプリ(app_id 一致)か確かめる
2. `POST /api/shutdown`(トークン付き)。**印刷中は 409 で断る**(`--force` で中断して停止)
3. 応答しないときだけ、ロックに記録した **PID + 生成時刻** が一致するプロセスを止める
   (`python` というプロセス名で一括終了しない)
4. 記録した Excel / cscript が残っていれば片付ける

## 7. ログと後追い(VER1.6.0)

詳しくは [ログとなぜなぜ分析.md](ログとなぜなぜ分析.md)。

- 要求ごとに問い合わせ番号を振る(`app/__init__.py`)。応答の見出し `X-Request-Ref` と、断りの本文 `error.ref` に入る
- 業務の断り・失敗は、番号つきで `events_*.jsonl` に1行(種類・文・詳しい原因・対象・ファイル・プリンター…)
- 印刷は受け付けた要求の番号を最後まで使う(開始 → 1件ずつ → 結果)
- 保存先は設定で指定できる(`core/logging_utils.py`)。書けなければローカルへ退避し、5 分ごとに戻れるか試す

## 8. デスクトップ版(VER2.0.0)

詳しくは [デスクトップ版.md](デスクトップ版.md)。業務(`app/`・`core/`)はブラウザ版と同じもの。

- `InspectionSheet.exe`(Rust/Tauri、`src-tauri/`)が窓を持ち、`bridge.py` を子として起動する
- 画面の要求は `app://`(Windows は `http://app.localhost/`)で受け、`/static/` は exe が返し、
  それ以外は標準入出力で `bridge.py` → Flask(WSGI として呼ぶだけ)へ渡す。**ポートを使わない**
- 心拍による自動終了は使わない(窓の × と「終了」で終える)
- ブラウザ版と同時に動かさない: `core/instance_guard.py` と `src-tauri/src/instance.rs`(名前付きミューテックス)

## 9. セキュリティ

- 待ち受けは `127.0.0.1` のみ。Host ヘッダーがローカル以外なら拒否(DNS リバインディング対策)
- `/api/*` は起動ごとのトークン(`X-Tool-Token` または `?t=`)と同一オリジン(`Sec-Fetch-Site`)を確認
  (`/api/health` と `/api/alive` だけは識別情報と「受け取った」しか返さないので不要)
- CORS ヘッダーは一切返さない
- 画面とサーバー間は点検表の ID だけをやり取りし、任意のファイルパスは受け付けない
  (フォルダ設定の参照はフォルダ名と Excel のファイル名だけを返す)
- 断りの形は `{"error": {"code", "message"}}`。400 = 入力の形、422 = 業務として断る、409 = 別の処理が先
