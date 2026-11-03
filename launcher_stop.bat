@echo off
rem  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
rem  Keep everything above the chcp line ASCII; see start.bat for why.
chcp 932 >nul 2>&1
rem ===================================================================
rem  日報複合ツール 終了の入口(業務ツール統合ランチャー 1.7 以降が使う)
rem
rem  デスクトップ版(日報複合ツール.exe): 窓に「閉じて」と頼む(窓の × と同じ流れ。
rem    打ちかけを置いてもらい、途中の処理があれば1つの確認。答えるまで閉じない)
rem  ブラウザ版: stop.bat と同じ(開いている画面に打ちかけを置いてもらってから止める)
rem
rem  終了コード: 0 = 止めた・動いていない / 3 = 処理中・確認中なので止めなかった /
rem    1 = そのほか止められなかった。標準出力は理由の1行だけ
rem    (1.8.9 は1行目、1.7 は最後の1行を理由として見せる)
rem  --force: 強制終了を選ばれたとき。処理の途中でも止める(4ツール・入口・デスクトップ版の
rem    外枠。窓に閉じてと頼み、閉じなければ exe を止める。途中の処理は捨てる)
rem  pause は置かない(/nopause・STOP_NOPAUSE=1 は受けて読み飛ばす)。詳しくは docs\ランチャー連携.md
rem ===================================================================
setlocal
pushd "%~dp0" || (
    echo このフォルダに移動できませんでした: "%~dp0"
    endlocal
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
