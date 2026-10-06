@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  ライン管理カレンダー (Web版) 停止
rem
rem  このアプリだけを止めます。同じPCで動く別の Python アプリに
rem  影響を与えません(基盤仕様書 2.8)。
rem ===================================================================
setlocal

rem  共有フォルダに置かれていても動くよう pushd を使う(start.bat と同じ)
pushd "%~dp0" || (
    echo [エラー] このフォルダに移動できませんでした: %~dp0
    pause
    exit /b 1
)
title ライン管理カレンダー - 停止

python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    goto :failed
)

python process_manager.py %*
if errorlevel 1 (
    echo.
    echo 止められなかったものがあります。上のメッセージを確認してください。
    echo Access と同期している最中のときは、中断してよければ次を実行してください:
    echo     stop.bat --force
    echo.
    goto :failed
)
popd
endlocal
exit /b 0

:failed
pause
popd
endlocal
exit /b 1
