@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  統合ツール ブラウザ版(予備)停止
rem
rem  ブラウザ版の入口と、入口が起こした各ツールのブラウザ版を止めます。
rem  同じPCで動く他の Python アプリは影響を受けません。
rem  デスクトップ版(統合ツール.exe)は止めません(窓の「終了」で閉じてください)。
rem ===================================================================
setlocal

rem  共有フォルダに置かれていても動くよう pushd を使う(start.bat と同じ)
pushd "%~dp0" || (
    echo [エラー] このフォルダに移動できませんでした: %~dp0
    pause
    exit /b 1
)
title 統合ツール - 停止

python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    goto :failed
)

rem  戻り値: 0 = 止めた / 1 = デスクトップ版が動いている(止めない) / 2 = 止めなかった
rem  (実行中の処理・画面で「閉じない」など) / 3 = Python が見つからない
python process_manager.py %*
set "code=%errorlevel%"
if not "%code%"=="0" (
    echo.
    echo 止められなかったものがあります。上のメッセージを確認してください。
    echo 実行中の処理があるときは、中断してよければ次を実行してください:
    echo     stop.bat --force
    echo.
    pause
)
popd
endlocal & exit /b %code%

:failed
pause
popd
endlocal
exit /b 3
