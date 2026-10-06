' ===================================================================
'  日報管理ツール (Web版) 起動
'
'  **この1つだけがダブルクリックする入り口です。**
'  コンソールを出さずに起動するので、画面はブラウザだけになります。
'  起動しないときは start.bat を使うと原因が表示されます。
'  (基盤仕様書 2.1「利用者向けの通常起動ファイルは1つに絞る」)
'
'  ラインの選択はアプリの画面の中で行います。以前の tkinter 版と同じく、
'  ラインごとに別々の端末で1つずつ起動して使います。
'
'  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
'  WSH reads a .vbs with the system ANSI code page, which is 932 on the
'  Japanese Windows this tool runs on. tests/test_launch_files.py checks it.
' ===================================================================
Option Explicit

Const APP_NAME = "日報管理ツール"

Dim shell, fso, here, script, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "start_app.py")

' 本体が同じフォルダにあるか。**pythonw はコンソールを持たない**ので、
' 無いまま起動すると本当に「何も起きない」。ここで気づけるようにする
' (このファイルだけをデスクトップにコピーすると、この状態になる)
If Not fso.FileExists(script) Then
    MsgBox "start_app.py が見つかりません。" & vbCrLf & vbCrLf & _
           "探した場所: " & script & vbCrLf & vbCrLf & _
           "このファイルは、アプリ一式が入ったフォルダの中から" & vbCrLf & _
           "実行してください(ショートカットを作るのは大丈夫です)。", _
           vbCritical, APP_NAME
    WScript.Quit 1
End If

' Python があるかを先に確かめる。無いまま起動すると、
' コンソールが出ないぶん「何も起きない」ように見えてしまう
If shell.Run("cmd /c python --version", 0, True) <> 0 Then
    MsgBox "Python が見つかりません。" & vbCrLf & vbCrLf & _
           "https://www.python.org/downloads/ からインストールし、" & vbCrLf & _
           "インストーラの最初の画面で「Add python.exe to PATH」に" & vbCrLf & _
           "チェックを入れてください。" & vbCrLf & vbCrLf & _
           "詳しい原因を見るには start.bat を実行してください。", _
           vbCritical, APP_NAME
    WScript.Quit 1
End If

' 実際に使うのは pythonw のほう。python はあるのに pythonw が無い入れ方も
' あるので、走らせる前に確かめる(ここを飛ばすと無言で失敗する)
If shell.Run("cmd /c pythonw --version", 0, True) <> 0 Then
    MsgBox "pythonw が見つかりません。" & vbCrLf & vbCrLf & _
           "Python は入っていますが、画面を出さずに起動するための" & vbCrLf & _
           "pythonw.exe がありません。" & vbCrLf & _
           "start.bat から起動してください(コンソールが開きます)。", _
           vbCritical, APP_NAME
    WScript.Quit 1
End If

' pythonw はコンソールを出さない。起動後もサーバが動き続けるので
' 待たずに抜ける(False)。失敗の通知は start_app.py が
' ブラウザにエラー画面を出して行う。
' パスは**絶対パスで渡す**。共有フォルダから実行されることがあり、
' 作業フォルダに頼ると見つけられないことがある
shell.CurrentDirectory = here
cmd = "pythonw " & Chr(34) & script & Chr(34)
shell.Run cmd, 0, False
