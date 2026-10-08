@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  統合ツール 終了の入口(業務ツール統合ランチャー 1.7 以降が使う)
rem
rem  デスクトップ版(統合ツール.exe): 窓に「閉じて」と頼む(窓の × と同じ流れ。
rem    打ちかけを置いてもらい、途中の処理があれば1つの確認。答えるまで閉じない)
rem  ブラウザ版: stop.bat と同じ(開いている画面に打ちかけを置いてもらってから止める)
rem
rem  終了コード: 0 = 終了の手続きをした / 1 = 止めなかった(最後の1行が理由)
rem  --force: 強制終了を選ばれたとき(ブラウザ版は処理の途中でも止める。
rem    デスクトップ版はランチャーの止め方に任せる)。pause は置かない
rem ===================================================================
setlocal
pushd "%~dp0" || (
    echo このフォルダに移動できませんでした: %~dp0
    exit /b 1
)
python process_manager.py --launcher %*
set "code=%errorlevel%"
popd
if "%code%"=="9009" (
    echo Python が見つかりません
    endlocal & exit /b 1
)
endlocal & exit /b %code%
