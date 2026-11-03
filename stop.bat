@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  日報複合ツール 停止
rem
rem  ブラウザ版の入口と、入口が起こした各ツールのブラウザ版を止めます。
rem  同じPCで動く他の Python アプリは影響を受けません。
rem  デスクトップ版(日報複合ツール.exe)は閉じません(窓の「終了」で閉じてください)。
rem  --force のときだけ、デスクトップ版も止めます(窓に閉じてと頼み、閉じなければ exe を止める)。
rem
rem  終了コード: 0 = 止めた・動いていない / 3 = 処理中なので止めなかった
rem    (途中の処理・画面で確認中・画面で「閉じない」) / 1 = そのほか止められなかった
rem    (デスクトップ版が動いている・Python が見つからない など)。
rem    止めなかったときは標準出力の1行目が理由(どのツールの何の処理か)
rem  stop.bat --force   処理中でも止める(4ツール・入口・デスクトップ版の外枠。途中の処理は捨てます)
rem  stop.bat /nopause  最後の pause を飛ばす(環境変数 STOP_NOPAUSE=1 でも同じ)
rem  詳しくは docs\ランチャー連携.md
rem ===================================================================
setlocal

rem  pause を飛ばすか(業務ツール統合ランチャーは STOP_NOPAUSE=1 を付けて呼ぶ)
set "NOPAUSE="
if /i "%STOP_NOPAUSE%"=="1" set "NOPAUSE=1"
for %%A in (%*) do if /i "%%~A"=="/nopause" set "NOPAUSE=1"

rem  共有フォルダに置かれていても動くよう pushd を使う(start.bat と同じ)
pushd "%~dp0" || (
    echo [エラー] このフォルダに移動できませんでした: "%~dp0"
    if not defined NOPAUSE pause
    endlocal
    exit /b 1
)
title 日報複合ツール - 停止

python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    if not defined NOPAUSE pause
    popd
    endlocal
    exit /b 1
)

python process_manager.py %*
set "code=%errorlevel%"
if not "%code%"=="0" (
    echo.
    echo 止められなかったものがあります。上のメッセージを確認してください。
    echo 実行中の処理があるときは、中断してよければ次を実行してください:
    echo     stop.bat --force
    echo.
    if not defined NOPAUSE pause
)
popd
endlocal & exit /b %code%
