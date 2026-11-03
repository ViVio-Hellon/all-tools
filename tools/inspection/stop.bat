@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  点検表 選択・印刷 停止
rem
rem  このアプリだけを止めます。同じPCで動く他の Python アプリや、
rem  利用者が自分で開いている Excel は影響を受けません(基盤仕様書 2.8)。
rem  このアプリが起動した Excel が残っていれば、それだけを終了します。
rem
rem  終了コード: 0 = 止めた・動いていない / 3 = 処理中なので止めなかった /
rem    1 = そのほか止められなかった。止めなかったときは標準出力の1行目が理由
rem    (業務ツール統合ランチャーの一覧と利用者への知らせに出る)
rem  stop.bat --force   処理中でも止める(途中の処理は捨てます)
rem  stop.bat /nopause  最後の pause を飛ばす(環境変数 STOP_NOPAUSE=1 でも同じ)
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
title 点検表 選択・印刷 - 停止

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
    echo 印刷中のときは、中断してよければ次を実行してください:
    echo     stop.bat --force
    echo.
    if not defined NOPAUSE pause
)
popd
endlocal & exit /b %code%
