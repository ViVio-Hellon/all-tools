' ===================================================================
'  日報複合ツール ショートカットを作る(配った先の PC で1回押す)
'
'  ツールのフォルダ(この scripts の1つ上)に、次の2つを作ります。
'    <名前>(ブラウザ版).lnk     … Start.vbs を指す
'    <名前>(デスクトップ版).lnk … 日報複合ツール.exe を指す(アイコンも exe のもの)
'  <名前> は config\app.json の display_name(読めなければ「日報複合ツール」)。
'
'  指す先は、押したときのこのフォルダの場所です。何度押しても作り直すだけ。
'  フォルダを移したら、もう一度押してください。
'  exe が無いフォルダでは、デスクトップ版のショートカットは作りません(結果の窓で知らせる)。
'  Windows の機能(WSH)だけで動きます。Python や追加のライブラリは要りません。
'
'  --- keep this file in CP932 (Shift-JIS) with CRLF line endings ---
'  WSH reads a .vbs with the system ANSI code page, which is 932 on the
'  Japanese Windows this tool runs on. tests/test_launch_files.py checks it.
' ===================================================================
Option Explicit

Const DEFAULT_NAME = "日報複合ツール"
Const BROWSER_LABEL = "(ブラウザ版)"
Const DESKTOP_LABEL = "(デスクトップ版)"

' デスクトップ版の exe。配るときの名前が先(scripts\make_dist.py の EXE_NAME)。
' AllTools.exe は GitHub Actions で作ったままの名前、統合ツール.exe は 1.4.0 で名前を変える前のもの
Dim EXE_NAMES
EXE_NAMES = Array("日報複合ツール.exe", "AllTools.exe", "統合ツール.exe")

Dim shell, fso, root, appName, starter, exe, name, report
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' ツールのフォルダ = この scripts の1つ上
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
starter = fso.BuildPath(root, "Start.vbs")
appName = ReadDisplayName(fso.BuildPath(root, "config\app.json"))

' 置き場所が違う(scripts だけをコピーした など)なら、何も作らずに知らせる
If Not fso.FileExists(starter) Then
    MsgBox "Start.vbs が見つからないため、ショートカットを作りませんでした。" & vbCrLf & vbCrLf & _
           "探した場所: " & starter & vbCrLf & vbCrLf & _
           "このファイルは、配布したフォルダの中の scripts から実行してください" & vbCrLf & _
           "(scripts だけを別の場所にコピーすると、この状態になります)。", _
           vbCritical, appName
    WScript.Quit 1
End If

report = ""

' --- ブラウザ版(Start.vbs) ---
On Error Resume Next
MakeShortcut fso.BuildPath(root, appName & BROWSER_LABEL & ".lnk"), starter, "", _
             appName & " ブラウザ版(予備)を起動します"
If Err.Number <> 0 Then
    report = report & "× " & appName & BROWSER_LABEL & " … 作れませんでした: " & Err.Description & vbCrLf
    Err.Clear
Else
    report = report & "○ " & appName & BROWSER_LABEL & " … Start.vbs を指します" & vbCrLf
End If
On Error GoTo 0

' --- デスクトップ版(exe) ---
exe = ""
For Each name In EXE_NAMES
    If exe = "" And fso.FileExists(fso.BuildPath(root, name)) Then exe = fso.BuildPath(root, name)
Next

Dim desktopLink
desktopLink = fso.BuildPath(root, appName & DESKTOP_LABEL & ".lnk")
If exe = "" Then
    ' 前に作ったものが残っていると、無い exe を指したままになるので消す
    If fso.FileExists(desktopLink) Then
        On Error Resume Next
        fso.DeleteFile desktopLink, True
        On Error GoTo 0
    End If
    report = report & "― " & appName & DESKTOP_LABEL & " … 作りませんでした。" & vbCrLf & _
             "    このフォルダに " & EXE_NAMES(0) & " がありません(ブラウザ版だけで使えます)。" & vbCrLf
Else
    On Error Resume Next
    MakeShortcut desktopLink, exe, exe & ",0", appName & " デスクトップ版を起動します"
    If Err.Number <> 0 Then
        report = report & "× " & appName & DESKTOP_LABEL & " … 作れませんでした: " & Err.Description & vbCrLf
        Err.Clear
    Else
        report = report & "○ " & appName & DESKTOP_LABEL & " … " & fso.GetFileName(exe) & " を指します" & vbCrLf
    End If
    On Error GoTo 0
End If

MsgBox "ショートカットを作りました(前からあったものは作り直しました)。" & vbCrLf & vbCrLf & _
       report & vbCrLf & _
       "作った場所: " & root & vbCrLf & vbCrLf & _
       "フォルダを移したときは、もう一度これを押してください。", _
       vbInformation, appName


' 1つ作る(同じ名前のものは上書き)。作業フォルダはツールのフォルダ
Sub MakeShortcut(linkPath, target, icon, description)
    Dim link
    Set link = shell.CreateShortcut(linkPath)
    link.TargetPath = target
    link.WorkingDirectory = root
    link.Description = description
    If icon <> "" Then link.IconLocation = icon
    link.WindowStyle = 1
    link.Save
End Sub

' config\app.json の display_name(UTF-8)。読めなければ既定の名前
Function ReadDisplayName(path)
    ReadDisplayName = DEFAULT_NAME
    If Not fso.FileExists(path) Then Exit Function
    Dim stream, text, re, found
    On Error Resume Next
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 2
    stream.Charset = "utf-8"
    stream.Open
    stream.LoadFromFile path
    text = stream.ReadText
    stream.Close
    If Err.Number <> 0 Then
        Err.Clear
        Exit Function
    End If
    On Error GoTo 0
    Set re = New RegExp
    re.Pattern = """display_name""\s*:\s*""([^""\\/:*?<>|]+)"""
    Set found = re.Execute(text)
    If found.Count > 0 Then ReadDisplayName = Trim(found(0).SubMatches(0))
End Function
