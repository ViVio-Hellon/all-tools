' ============================================================================
' excel_worker.vbs - Excel COM worker for the inspection sheet print system
'
'   * ASCII ONLY. Do not add Japanese text to this file (cscript reads it as
'     ANSI). Japanese values travel as hex (UTF-16 code units) instead.
'   * Called by app/services/excel_service.py:
'       cscript //nologo //E:vbscript excel_worker.vbs session
'   * One worker (and one hidden Excel) serves many jobs in a row: starting
'     Excel takes seconds, so it is kept while jobs keep coming. Python
'     closes stdin after an idle period; then Excel quits and the worker ends.
'     Each job has mode = preview | print | selftest.
'   * NO FILES are exchanged with Python. Python from the Microsoft Store
'     writes %LOCALAPPDATA% / %TEMP% to a private copy that cscript and
'     Excel cannot see ("path not found", 0x4C). Everything goes through
'     stdin / stdout instead.
'
' Protocol (repeated for each job)
'   stdin (from Python)
'     SET <key> <hex>         (job values: mode, file.<i>, copies, ...)
'     ENDJOB
'     (stdin closed = no more jobs: quit Excel, print BYE, exit)
'   stdout (ASCII, one command per line)        stdin (reply from Python)
'     READY <mode>
'     EXCEL <hwnd>            (Python records the Excel PID from this)
'     OPENED <i>
'     COPIED 1                              ->  OK | EXPORT | RECOPY | SKIP
'                                               (RECOPY: someone else replaced the
'                                                clipboard; copy again)
'     EXPORTED 1 | EXPORT_FAILED 1
'     NEXT <i>                              ->  GO | STOP
'     BEGIN <i>
'     END <i> OK|NG
'     FINISH <rc>             (end of one job; Excel stays for the next one)
'     BYE                     (Excel has quit; the worker ends)
'     JOB <n>                 (number of job keys read)
'     RESULT <key> <hex>      (item.<i>.code, item.<i>.message, ...)
'     PNGDATA <base64>        (chart export fallback, several lines)
'   <hex> = UTF-16 code units as 4 hex digits each ("AB" -> "00410042")
'
' Ported from the VBA version:
'   PrintWorkbooksFromFolder  -> RunPrint / PrintOne
'   UFPreview.GeneratePreview -> RunPreview / GetPreviewRange / CopyRangePicture
' ============================================================================
Option Explicit

Const xlSheetVisible = -1
Const xlPicture = -4147
Const xlR1C1 = -4150
Const xlA1 = 1
Const xlNone = -4142
Const msoAutomationSecurityForceDisable = 3
Const adTypeBinary = 1
Const TemporaryFolder = 2

Const RC_OK = 0
Const RC_ARGS = 2
Const RC_EXCEL = 3
Const RC_ITEM = 4

Dim gFso, gJob, gXl, gMode, gLastErrNumber, gLastErrDesc, gJobError

Set gFso = CreateObject("Scripting.FileSystemObject")
Set gXl = Nothing
gLastErrNumber = 0
gLastErrDesc = ""
gJobError = ""

WScript.Quit Main()


' ----------------------------------------------------------------------------
' Entry point
' ----------------------------------------------------------------------------
Function Main()
    Dim rc
    Main = RC_OK
    Do
        gJobError = ""
        Set gJob = ReadJob()
        If Len(gJobError) > 0 And gJob.Count = 0 Then Exit Do   ' stdin closed: no more jobs
        gMode = LCase(JobValue("mode", ""))
        Emit "READY " & gMode
        Emit "JOB " & gJob.Count
        If Len(gJobError) > 0 Then
            WriteResult "fatal.code", "JOB_READ_FAILED"
            WriteResult "fatal.message", gJobError
            Emit "FINISH " & RC_ARGS
            Exit Do
        End If

        Select Case gMode
            Case "preview"
                rc = RunPreview()
            Case "print"
                rc = RunPrint()
            Case "selftest"
                rc = RunSelfTest()
            Case Else
                WriteResult "fatal.code", "BAD_MODE"
                rc = RC_ARGS
        End Select

        CloseAllBooks
        Emit "FINISH " & rc
        Main = rc
    Loop
    QuitExcel
    Emit "BYE"
End Function


' ----------------------------------------------------------------------------
' Preview: first visible sheet -> print area -> CopyPicture -> clipboard
' ----------------------------------------------------------------------------
Function RunPreview()
    Dim path, wb, ws, rng, how, cmd, attempt

    RunPreview = RC_ITEM
    path = JobValue("file.1", "")
    If Not gFso.FileExists(path) Then
        SetItemError 1, "FILE_NOT_FOUND", 0, path
        Exit Function
    End If
    If Not StartExcel() Then
        RunPreview = RC_EXCEL
        Exit Function
    End If

    Set wb = OpenBook(1, path)
    If wb Is Nothing Then Exit Function

    Set ws = FirstVisibleSheet(wb)
    If ws Is Nothing Then
        SetItemError 1, "NO_VISIBLE_SHEET", 0, ""
        CloseBook wb
        Exit Function
    End If
    WriteResult "item.1.sheet", SafeName(ws)

    how = ""
    Set rng = GetPreviewRange(ws, how)
    If rng Is Nothing Then
        SetItemError 1, "RANGE_FAILED", gLastErrNumber, gLastErrDesc
        CloseBook wb
        Exit Function
    End If
    WriteResult "item.1.range", RangeAddress(rng)
    WriteResult "item.1.range_source", how

    If Not CopyRangePicture(ws, rng) Then
        SetItemError 1, "COPY_FAILED", gLastErrNumber, gLastErrDesc
        CloseBook wb
        Exit Function
    End If

    ' Python reads the clipboard while Excel is still alive (delayed rendering)
    Emit "COPIED 1"
    cmd = WaitCommand()
    attempt = 1
    Do While cmd = "RECOPY" And attempt < 3
        attempt = attempt + 1
        WriteResult "log", "Clipboard replaced by another program; copy again (" & attempt & ")"
        If Not CopyRangePicture(ws, rng) Then
            SetItemError 1, "COPY_FAILED", gLastErrNumber, gLastErrDesc
            CloseBook wb
            Exit Function
        End If
        Emit "COPIED 1"
        cmd = WaitCommand()
    Loop
    If cmd = "EXPORT" Then
        If ExportViaChart(rng) Then
            Emit "EXPORTED 1"
            RunPreview = RC_OK
        Else
            Emit "EXPORT_FAILED 1"
            SetItemError 1, "COPY_FAILED", gLastErrNumber, gLastErrDesc
        End If
    ElseIf cmd = "OK" Then
        RunPreview = RC_OK
    End If

    ClearCopyMode
    CloseBook wb
End Function


' Same rules as UFPreview.GeneratePreview in the VBA version:
'   1) print area (first area only)  2) UsedRange limited to max rows/cols
'   3) A1:P40
Function GetPreviewRange(ws, how)
    Dim rng, addr, maxRows, maxCols, refStyle, okFlag

    Set GetPreviewRange = Nothing
    maxRows = CLng(JobValue("max_rows", "40"))
    maxCols = CLng(JobValue("max_cols", "16"))
    On Error Resume Next

    ' 1) Print area as a defined name (independent of A1/R1C1 style)
    Set rng = Nothing
    Set rng = ws.Names("Print_Area").RefersToRange
    If Err.Number <> 0 Then
        Err.Clear
        Set rng = Nothing
    End If
    If Not rng Is Nothing Then
        Set rng = rng.Areas(1)
        okFlag = (Err.Number = 0)
        Err.Clear
        If okFlag Then
            how = "print_area"
            Set GetPreviewRange = rng
            Exit Function
        End If
        Set rng = Nothing
    End If

    ' 2) PageSetup.PrintArea text (convert R1C1 to A1 when needed)
    addr = ""
    addr = ws.PageSetup.PrintArea
    If Err.Number <> 0 Then
        Err.Clear
        addr = ""
    End If
    If Len(addr) > 0 Then
        If InStr(addr, ",") > 0 Then addr = Split(addr, ",")(0)
        refStyle = 0
        refStyle = gXl.ReferenceStyle
        Err.Clear
        If refStyle = xlR1C1 Then
            addr = gXl.ConvertFormula(addr, xlR1C1, xlA1)
            Err.Clear
        End If
        Set rng = Nothing
        Set rng = ws.Range(addr)
        okFlag = (Err.Number = 0)
        Err.Clear
        If okFlag Then
            If Not rng Is Nothing Then
                how = "print_area_text"
                Set GetPreviewRange = rng
                Exit Function
            End If
        End If
        Set rng = Nothing
    End If

    ' 3) UsedRange (max 40 rows x 16 columns in the VBA version)
    Set rng = Nothing
    Set rng = ws.UsedRange
    If Err.Number <> 0 Then
        Err.Clear
        Set rng = Nothing
    End If
    If rng Is Nothing Then
        Set rng = ws.Range("A1:P40")
        how = "default"
    Else
        how = "used_range"
        If rng.Rows.Count > maxRows Then Set rng = rng.Resize(maxRows, rng.Columns.Count)
        If rng.Columns.Count > maxCols Then Set rng = rng.Resize(rng.Rows.Count, maxCols)
    End If
    If Err.Number <> 0 Then
        gLastErrNumber = Err.Number
        gLastErrDesc = Err.Description
        Err.Clear
        Set rng = Nothing
    End If
    Set GetPreviewRange = rng
End Function


Function CopyRangePicture(ws, rng)
    Dim attempt, appearance

    CopyRangePicture = False
    appearance = CInt(JobValue("appearance", "1"))
    On Error Resume Next
    ' Same as the VBA version: screen updating on, activate sheet, select range
    gXl.ScreenUpdating = True
    ws.Activate
    rng.Select
    Err.Clear
    For attempt = 1 To 5
        Err.Clear
        rng.CopyPicture appearance, xlPicture
        If Err.Number = 0 Then
            CopyRangePicture = True
            Exit Function
        End If
        gLastErrNumber = Err.Number
        gLastErrDesc = Err.Description
        WriteResult "log", "CopyPicture retry " & attempt & ": " & gLastErrDesc
        Err.Clear
        WScript.Sleep 300 * attempt
    Next
End Function


' Fallback when Python cannot read the clipboard: paste into a chart in a
' temporary workbook and export PNG to this script's own TEMP folder, then
' send it to Python as base64 (PNGDATA lines). The inspection workbook is
' not touched.
Function ExportViaChart(rng)
    Dim tmpWb, co, exported, outPath

    ExportViaChart = False
    On Error Resume Next
    outPath = gFso.BuildPath(gFso.GetSpecialFolder(TemporaryFolder).Path, _
                             "isp_" & Replace(gFso.GetTempName(), ".tmp", ".png"))
    Set tmpWb = gXl.Workbooks.Add
    If Err.Number <> 0 Then
        gLastErrNumber = Err.Number
        gLastErrDesc = Err.Description
        Err.Clear
        Exit Function
    End If
    Set co = tmpWb.Worksheets(1).ChartObjects.Add(0, 0, rng.Width, rng.Height)
    co.Activate
    co.Chart.ChartArea.Border.LineStyle = xlNone
    Err.Clear
    co.Chart.Paste
    If Err.Number = 0 Then
        exported = co.Chart.Export(outPath, "PNG")
    End If
    If Err.Number <> 0 Then
        gLastErrNumber = Err.Number
        gLastErrDesc = Err.Description
        WriteResult "log", "Chart export failed: " & gLastErrDesc
    End If
    Err.Clear
    tmpWb.Close False
    Err.Clear
    If gFso.FileExists(outPath) Then
        ExportViaChart = EmitFileBase64(outPath)
        gFso.DeleteFile outPath, True
        Err.Clear
    End If
End Function


' Send a binary file as base64 lines: "PNGDATA <chunk>".
' ADODB.Stream + MSXML are part of Windows.
Function EmitFileBase64(path)
    Dim stm, doc, el, b64, i

    EmitFileBase64 = False
    On Error Resume Next
    Set stm = CreateObject("ADODB.Stream")
    stm.Type = adTypeBinary
    stm.Open
    stm.LoadFromFile path
    Set doc = CreateObject("MSXML2.DOMDocument")
    Set el = doc.createElement("b")
    el.dataType = "bin.base64"
    el.nodeTypedValue = stm.Read
    stm.Close
    b64 = Replace(Replace(el.Text, vbCr, ""), vbLf, "")
    If Err.Number <> 0 Then
        gLastErrNumber = Err.Number
        gLastErrDesc = Err.Description
        Err.Clear
        WriteResult "log", "base64 failed: " & gLastErrDesc
        Exit Function
    End If
    For i = 1 To Len(b64) Step 3000
        Emit "PNGDATA " & Mid(b64, i, 3000)
    Next
    EmitFileBase64 = (Len(b64) > 0)
End Function


' ----------------------------------------------------------------------------
' Print: open read-only -> first visible sheet -> PrintOut Copies
' Print settings (area, paper, orientation, margins, breaks, header/footer)
' are taken from the workbook as they are. Nothing is changed.
' ----------------------------------------------------------------------------
Function RunPrint()
    Dim count, copies, i, cmd, failed

    RunPrint = RC_OK
    count = CLng(JobValue("count", "0"))
    copies = CLng(JobValue("copies", "1"))
    If copies < 1 Then copies = 1
    If copies > 100 Then copies = 100
    If count < 1 Then
        WriteResult "fatal.code", "JOB_READ_FAILED"
        WriteResult "fatal.message", "count=" & JobValue("count", "(missing)")
        RunPrint = RC_ARGS
        Exit Function
    End If
    If Not StartExcel() Then
        RunPrint = RC_EXCEL
        Exit Function
    End If

    failed = 0
    For i = 1 To count
        Emit "NEXT " & i
        cmd = WaitCommand()
        If cmd <> "GO" Then
            WriteResult "stopped_at", i
            WriteResult "stopped_by", cmd
            Exit For
        End If
        Emit "BEGIN " & i
        If PrintOne(i, JobValue("file." & i, ""), copies) Then
            Emit "END " & i & " OK"
        Else
            failed = failed + 1
            Emit "END " & i & " NG"
        End If
    Next
    If failed > 0 Then RunPrint = RC_ITEM
End Function


Function PrintOne(index, path, copies)
    Dim wb, ws, number, description

    PrintOne = False
    Set wb = OpenBook(index, path)
    If wb Is Nothing Then Exit Function

    Set ws = FirstVisibleSheet(wb)
    If ws Is Nothing Then
        SetItemError index, "NO_VISIBLE_SHEET", 0, ""
        CloseBook wb
        Exit Function
    End If
    WriteResult "item." & index & ".sheet", SafeName(ws)

    number = 0
    description = ""
    If DoPrintOut(ws, copies, number, description) Then
        PrintOne = True
    ElseIf IsBusyError(number) Then
        SetItemError index, "EXCEL_BUSY", number, description
    Else
        SetItemError index, "PRINT_FAILED", number, description
    End If
    CloseBook wb
End Function


Function DoPrintOut(ws, copies, number, description)
    On Error Resume Next
    Err.Clear
    ws.PrintOut , , copies
    number = Err.Number
    description = Err.Description
    Err.Clear
    DoPrintOut = (number = 0)
End Function


Function RunSelfTest()
    If StartExcel() Then
        RunSelfTest = RC_OK
    Else
        RunSelfTest = RC_EXCEL
    End If
End Function


' ----------------------------------------------------------------------------
' Excel application
' ----------------------------------------------------------------------------
Function StartExcel()
    Dim disableMacros, hwnd, number, description

    StartExcel = False
    On Error Resume Next
    If ExcelAlive() Then
        WriteResult "excel.version", gXl.Version
        WriteResult "excel.reused", "1"
        Err.Clear
        StartExcel = True
        Exit Function
    End If
    Set gXl = CreateObject("Excel.Application")
    If Err.Number <> 0 Then
        ' Copy the error first: WriteResult clears Err (On Error Resume Next)
        number = Err.Number
        description = Err.Description
        Err.Clear
        WriteResult "fatal.code", "EXCEL_CREATE_FAILED"
        WriteResult "fatal.errno", "0x" & Hex(number)
        WriteResult "fatal.message", description
        Set gXl = Nothing
        Exit Function
    End If
    If gXl Is Nothing Then
        WriteResult "fatal.code", "EXCEL_CREATE_FAILED"
        Exit Function
    End If

    ' Report the window handle first so that Python can always clean up
    hwnd = 0
    hwnd = gXl.Hwnd
    Err.Clear
    Emit "EXCEL " & CStr(hwnd)

    gXl.Visible = False
    ' Files the user double-clicks must not open in this hidden instance.
    ' Reset before Quit: Excel saves this option (QuitExcel).
    gXl.IgnoreRemoteRequests = True
    gXl.DisplayAlerts = False
    gXl.AskToUpdateLinks = False
    gXl.ScreenUpdating = False
    Err.Clear
    disableMacros = (JobValue("disable_macros", "1") = "1")
    If disableMacros Then
        gXl.EnableEvents = False
        gXl.AutomationSecurity = msoAutomationSecurityForceDisable
        Err.Clear
    End If
    WriteResult "excel.version", gXl.Version
    Err.Clear
    StartExcel = True
End Function


Sub QuitExcel()
    Dim i
    On Error Resume Next
    If gXl Is Nothing Then Exit Sub
    gXl.DisplayAlerts = False
    For i = gXl.Workbooks.Count To 1 Step -1
        gXl.Workbooks(i).Close False
    Next
    gXl.IgnoreRemoteRequests = False
    gXl.Quit
    Set gXl = Nothing
    Err.Clear
End Sub


' The Excel started earlier is still usable (the user may have ended it).
Function ExcelAlive()
    Dim v
    ExcelAlive = False
    If gXl Is Nothing Then Exit Function
    On Error Resume Next
    v = ""
    v = gXl.Version
    If Err.Number = 0 And Len(v) > 0 Then
        ExcelAlive = True
    Else
        Set gXl = Nothing
    End If
    Err.Clear
End Function


' Between jobs: close every workbook but keep Excel for the next job.
Sub CloseAllBooks()
    Dim i
    On Error Resume Next
    If gXl Is Nothing Then Exit Sub
    gXl.CutCopyMode = False
    For i = gXl.Workbooks.Count To 1 Step -1
        gXl.Workbooks(i).Close False
    Next
    gXl.ScreenUpdating = False
    Err.Clear
End Sub


' Workbooks.Open(FileName, UpdateLinks, ReadOnly, Format, Password,
'   WriteResPassword, IgnoreReadOnlyRecommended, Origin, Delimiter,
'   Editable, Notify, Converter, AddToMru)
' A dummy password is passed so that a password protected workbook fails
' with an error instead of waiting for input in the hidden Excel.
Function OpenBook(index, path)
    Dim wb, pwd, attempt, number, description

    Set OpenBook = Nothing
    If Not gFso.FileExists(path) Then
        SetItemError index, "FILE_NOT_FOUND", 0, path
        Exit Function
    End If
    pwd = JobValue("open_password", "")
    number = 0
    description = ""
    On Error Resume Next
    For attempt = 1 To 3
        Err.Clear
        Set wb = Nothing
        If Len(pwd) > 0 Then
            Set wb = gXl.Workbooks.Open(path, 0, True, , pwd, , True, , , False, False, , False)
        Else
            Set wb = gXl.Workbooks.Open(path, 0, True, , , , True, , , False, False, , False)
        End If
        number = Err.Number
        description = Err.Description
        Err.Clear
        If number = 0 Then Exit For
        If Not IsBusyError(number) Then Exit For
        WScript.Sleep 1000 * attempt
    Next
    If number = 0 Then
        If wb Is Nothing Then number = -1
    End If
    If number <> 0 Then
        If IsBusyError(number) Then
            SetItemError index, "EXCEL_BUSY", number, description
        Else
            SetItemError index, "OPEN_FAILED", number, description
        End If
        Exit Function
    End If
    Emit "OPENED " & index
    Set OpenBook = wb
End Function


Sub CloseBook(wb)
    On Error Resume Next
    If Not wb Is Nothing Then wb.Close False
    Err.Clear
End Sub


' Same as the VBA version: the first worksheet whose Visible = xlSheetVisible
Function FirstVisibleSheet(wb)
    Dim ws, vis
    Set FirstVisibleSheet = Nothing
    On Error Resume Next
    For Each ws In wb.Worksheets
        vis = 0
        vis = ws.Visible
        If vis = xlSheetVisible Then
            Set FirstVisibleSheet = ws
            Exit For
        End If
    Next
    Err.Clear
End Function


Sub ClearCopyMode()
    On Error Resume Next
    gXl.CutCopyMode = False
    Err.Clear
End Sub


Function SafeName(obj)
    SafeName = ""
    On Error Resume Next
    SafeName = obj.Name
    Err.Clear
End Function


Function RangeAddress(rng)
    RangeAddress = ""
    On Error Resume Next
    RangeAddress = rng.Address(False, False)
    Err.Clear
End Function


' RPC_E_SERVERCALL_RETRYLATER (0x8001010A) / RPC_E_CALL_REJECTED (0x80010001)
Function IsBusyError(number)
    IsBusyError = (number = -2147417846 Or number = -2147418111)
End Function


' ----------------------------------------------------------------------------
' Communication with Python
' ----------------------------------------------------------------------------
Sub Emit(text)
    On Error Resume Next
    WScript.StdOut.WriteLine text
    Err.Clear
End Sub


Function WaitCommand()
    Dim line
    On Error Resume Next
    line = WScript.StdIn.ReadLine
    If Err.Number <> 0 Then
        gLastErrNumber = Err.Number
        gLastErrDesc = Err.Description
        Err.Clear
        WriteResult "log", "StdIn.ReadLine failed: 0x" & Hex(gLastErrNumber) & " " & gLastErrDesc
        WaitCommand = "EOF"
        Exit Function
    End If
    WaitCommand = UCase(Trim(line))
End Function


' Job values come on stdin: "SET <key> <hex>" lines, then "ENDJOB".
Function ReadJob()
    Dim d, line, parts
    Set d = CreateObject("Scripting.Dictionary")
    d.CompareMode = 1
    Set ReadJob = d
    On Error Resume Next
    Do
        line = WScript.StdIn.ReadLine
        If Err.Number <> 0 Then
            gJobError = "StdIn.ReadLine 0x" & Hex(Err.Number) & " " & Err.Description & _
                        " (keys read: " & d.Count & ")"
            Err.Clear
            Exit Function
        End If
        line = Trim(line)
        If line = "ENDJOB" Then Exit Function
        parts = Split(line, " ")
        If UBound(parts) >= 1 Then
            If parts(0) = "SET" Then
                If UBound(parts) >= 2 Then
                    d(parts(1)) = UnHexW(parts(2))
                Else
                    d(parts(1)) = ""
                End If
            End If
        End If
    Loop
End Function


Function JobValue(key, defaultValue)
    If gJob.Exists(key) Then
        JobValue = gJob(key)
    Else
        JobValue = defaultValue
    End If
End Function


Sub WriteResult(key, value)
    Dim text
    On Error Resume Next
    text = ""
    text = CStr(value)
    Emit "RESULT " & key & " " & HexW(text)
    Err.Clear
End Sub


Sub SetItemError(index, code, number, description)
    WriteResult "item." & index & ".code", code
    If number <> 0 Then WriteResult "item." & index & ".errno", "0x" & Hex(number)
    If Len(description) > 0 Then WriteResult "item." & index & ".message", description
End Sub


' UTF-16 code units as 4 hex digits each ("AB" -> "00410042").
' stdout is ASCII only (console code page), so Japanese is sent this way.
Function HexW(text)
    Dim i, code, s
    s = ""
    For i = 1 To Len(text)
        code = AscW(Mid(text, i, 1))
        If code < 0 Then code = code + 65536
        s = s & Right("000" & Hex(code), 4)
    Next
    HexW = s
End Function


Function UnHexW(text)
    Dim i, s
    s = ""
    For i = 1 To Len(text) - 3 Step 4
        s = s & ChrW(CLng("&H" & Mid(text, i, 4)))
    Next
    UnHexW = s
End Function
