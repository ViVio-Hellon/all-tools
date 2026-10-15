' ============================================================================
' excel_worker.vbs - Excel / Word COM worker for the inspection sheet print system
'
'   * ASCII ONLY. Do not add Japanese text to this file (cscript reads it as
'     ANSI). Japanese values travel as hex (UTF-16 code units) instead.
'   * Called by app/services/excel_service.py:
'       cscript //nologo //E:vbscript excel_worker.vbs session
'   * One worker (and one hidden Excel) serves many jobs in a row: starting
'     Excel takes seconds, so it is kept while jobs keep coming. Python
'     closes stdin after an idle period; then Excel quits and the worker ends.
'     Each job has mode = preview | print | selftest.
'   * Word documents (kind.<i> = word, decided by Python from the extension:
'     .doc / .docx / .docm) are opened in a hidden Word instead of Excel.
'     Word is started only when a Word document comes, and it is quit at the
'     end of every job (Word has no IgnoreRemoteRequests: a document the user
'     double-clicks may open in our hidden Word, so it is not kept running).
'     If the user's own document is open in it, Word is shown and left alone.
'     If CreateObject hands back a Word that was already running (the user's),
'     it is used as it is: not hidden, its settings given back, never quit.
'   * Word is driven the way the VBA version (PrintWordFile) drove it, which
'     printed on the line PCs: CreateObject, DisplayAlerts = none, Visible =
'     False, Documents.Open(path, ReadOnly:=True, AddToRecentFiles:=False,
'     Visible:=False), PrintOut Background:=False, Copies, Close without
'     saving (VER2.4.1). Nothing more is set on Word or asked of the hidden
'     document before printing: no AutomationSecurity / WordBasic macro
'     switch / FeatureInstall, no dummy password, no page count, no window
'     handle (the Word PID comes from WMI before any document is opened).
'   * Word ended under us (VBScript error 462 / RPC errors: it crashed, the
'     user closed it, or something ended it): Word is started again and the
'     job (preview) or the document (print) is tried ONCE more. Not when
'     Word ended while printing (part of it may be on paper already).
'   * Word preview = PDF (VER2.4.0): Document.ExportAsFixedFormat writes a PDF
'     to this script's own TEMP folder (%TEMP%\isp_preview\isp_pv_*.pdf); it
'     is sent as PDFDATA lines and deleted at once. The screen shows the PDF.
'     Only when that is not possible (no ExportAsFixedFormat in an old Word,
'     Word ended at it) does it fall back to page 1 as an EMF, then to the
'     clipboard. Leftovers of a worker that was ended (older than an hour)
'     are deleted when the worker starts.
'   * This PC: an inspection sheet left open in the user's Excel / Word is
'     closed WITHOUT SAVING first (close_open_copy = 1; the sheets are
'     originals, printed only). Only that file: other workbooks / documents
'     and the application stay. A file open on another PC cannot be closed
'     from here; every file is opened read-only, so it prints anyway.
'   * NO FILES are exchanged with Python. Python from the Microsoft Store
'     writes %LOCALAPPDATA% / %TEMP% to a private copy that cscript and
'     Excel cannot see ("path not found", 0x4C). Everything goes through
'     stdin / stdout instead (the PDF too: Word writes it, this script reads
'     it, sends it and deletes it; Python never sees the file).
'
' Protocol (repeated for each job)
'   stdin (from Python)
'     SET <key> <hex>         (job values: mode, file.<i>, copies, ...)
'     ENDJOB
'     (stdin closed = no more jobs: quit Excel, print BYE, exit)
'   stdout (ASCII, one command per line)        stdin (reply from Python)
'     READY <mode>
'     EXCEL <hwnd>            (Python records the Excel PID from this)
'     WORD <hwnd>             (Python records the Word PID from this)
'     WORDPID <pid>           (the Word just started, found before any document
'                              is opened; Python can end it if it hangs)
'     STAGE <name>            (the step that starts now: word.create, word.open,
'                              word.page, word.print, excel.open, ... Python
'                              tells where it stopped when nothing comes back)
'     WORDBYE 1|0             (Word quit / left running for the user's documents)
'     WORDSHARED 1            (CreateObject returned the user's running Word:
'                              Python must never end that process)
'     WORDLOST <stage>        (Word ended under us at that step; Python ends what
'                              is left of the Word this worker started)
'     RETRY <i>               (Word was started again; item <i> is tried once more)
'     OPENED <i>
'     COPIED 1                              ->  OK | EXPORT | RECOPY | SKIP
'                                               (RECOPY: someone else replaced the
'                                                clipboard; copy again)
'     EXPORTED 1 | EXPORT_FAILED 1
'     PDFDONE 1                             ->  OK | PAGE
'                                               (Word preview: the document as a
'                                                PDF in PDFDATA lines; PAGE = not
'                                                a PDF, draw page 1 as EMF instead)
'     EMFDONE 1                             ->  OK | COPY
'                                               (Word preview: page 1 as EMF in
'                                                EMFDATA lines; COPY = could not
'                                                draw it, copy page 1 instead)
'     NEXT <i>                              ->  GO | STOP
'     BEGIN <i>
'     END <i> OK|NG
'     FINISH <rc>             (end of one job; Excel stays for the next one)
'     BYE                     (Excel has quit; the worker ends)
'     JOB <n>                 (number of job keys read)
'     RESULT <key> <hex>      (item.<i>.code, item.<i>.errno, item.<i>.message,
'                              item.<i>.step, item.<i>.closed_open = Excel|Word
'                              when a copy open on this PC was closed, ...)
'     PNGDATA <base64>        (chart export fallback, several lines)
'     EMFDATA <base64>        (Word page 1 as an enhanced metafile, several lines)
'     PDFDATA <base64>        (Word document as a PDF, several lines)
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
Const adSaveCreateOverWrite = 2
Const TemporaryFolder = 2
Const wdAlertsNone = 0
Const wdDoNotSaveChanges = 0
Const wdPrintView = 3
Const wdGoToPage = 1
Const wdGoToAbsolute = 1
Const wdStatisticPages = 2
Const wdExportFormatPDF = 17
Const wdExportOptimizeForOnScreen = 1
Const wdExportAllDocument = 0
Const wdExportFromTo = 3
Const wdExportDocumentContent = 0
Const wdExportCreateNoBookmarks = 0

Const RC_OK = 0
Const RC_ARGS = 2
Const RC_EXCEL = 3
Const RC_ITEM = 4

Dim gFso, gJob, gXl, gWd, gWordReported, gMode, gLastErrNumber, gLastErrDesc, gJobError, gStage
Dim gWdShared, gWordLostStage, gSavedAlerts, gWordPid

Set gFso = CreateObject("Scripting.FileSystemObject")
Set gXl = Nothing
Set gWd = Nothing
gWordReported = False
gWdShared = False
gWordLostStage = ""
gWordPid = 0
gLastErrNumber = 0
gLastErrDesc = ""
gJobError = ""
gStage = ""

WScript.Quit Main()


' ----------------------------------------------------------------------------
' Entry point
' ----------------------------------------------------------------------------
Function Main()
    Dim rc
    Main = RC_OK
    SweepTempFiles
    Do
        gJobError = ""
        gStage = ""
        gLastErrNumber = 0
        gLastErrDesc = ""
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
        FinishWord
        Emit "FINISH " & rc
        Main = rc
    Loop
    QuitExcel
    FinishWord
    Emit "BYE"
End Function


' ----------------------------------------------------------------------------
' Preview: first visible sheet -> print area -> CopyPicture -> clipboard
' ----------------------------------------------------------------------------
Function RunPreview()
    Dim path, wb, ws, rng, how, cmd, attempt

    RunPreview = RC_ITEM
    If IsWord(1) Then
        RunPreview = RunPreviewWord()
        Exit Function
    End If
    path = JobValue("file.1", "")
    If Not gFso.FileExists(path) Then
        SetItemError 1, "FILE_NOT_FOUND", 0, path
        Exit Function
    End If
    CloseOpenCopy 1, path
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
    Stage "excel.copy"
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
    Stage "excel.export"
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
        ExportViaChart = EmitFileBase64(outPath, "PNGDATA")
        gFso.DeleteFile outPath, True
        Err.Clear
    End If
End Function


' Send bytes (Variant Byte()) as base64 lines: "<prefix> <chunk>".
' MSXML is part of Windows. No file is needed.
Function EmitBytesBase64(bytes, prefix)
    Dim doc, el, b64, i

    EmitBytesBase64 = False
    On Error Resume Next
    Set doc = CreateObject("MSXML2.DOMDocument")
    Set el = doc.createElement("b")
    el.dataType = "bin.base64"
    el.nodeTypedValue = bytes
    b64 = Replace(Replace(el.Text, vbCr, ""), vbLf, "")
    If Err.Number <> 0 Then
        LogErr "base64 (MSXML)"
        Exit Function
    End If
    For i = 1 To Len(b64) Step 3000
        Emit prefix & " " & Mid(b64, i, 3000)
    Next
    EmitBytesBase64 = (Len(b64) > 0)
End Function


' Send a binary file as base64 lines: "<prefix> <chunk>" (PNGDATA / EMFDATA).
' ADODB.Stream + MSXML are part of Windows.
Function EmitFileBase64(path, prefix)
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
        Emit prefix & " " & Mid(b64, i, 3000)
    Next
    EmitFileBase64 = (Len(b64) > 0)
End Function


' ----------------------------------------------------------------------------
' Print: open read-only -> first visible sheet -> PrintOut Copies
' Print settings (area, paper, orientation, margins, breaks, header/footer)
' are taken from the workbook as they are. Nothing is changed.
' ----------------------------------------------------------------------------
Function RunPrint()
    Dim count, copies, i, cmd, failed, path, excels, excelReady, ok

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
    ' This PC: inspection sheets left open in the user's Excel / Word are closed
    ' (without saving) before anything is started here.
    For i = 1 To count
        CloseOpenCopy i, JobValue("file." & i, "")
    Next
    ' Excel is started first as before (only when an Excel workbook is in the job).
    ' Word starts at the first Word document (StartWord).
    excels = 0
    For i = 1 To count
        If Not IsWord(i) Then excels = excels + 1
    Next
    excelReady = False
    If excels > 0 Then
        excelReady = StartExcel()
        If Not excelReady And excels = count Then
            RunPrint = RC_EXCEL
            Exit Function
        End If
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
        path = JobValue("file." & i, "")
        ok = False
        If IsWord(i) Then
            ok = PrintWordItem(i, path, copies)
        ElseIf excelReady Then
            ok = PrintOne(i, path, copies)
        Else
            SetItemError i, "EXCEL_CREATE_FAILED", 0, ""
        End If
        If ok Then
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
    Stage "excel.print"
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



' ============================================================================
' Word documents (.doc / .docx / .docm)
'   Print : open read-only -> PrintOut Copies (the document's own settings)
'   Preview: the document as a PDF (ExportAsFixedFormat -> PDFDATA, VER2.4.0).
'            If that is not possible: page 1 of the print layout as an
'            enhanced metafile (EMFDATA). If Python cannot draw it, page 1
'            is copied to the clipboard and read the same way as an Excel
'            range (COPIED).
'
' Diagnostics (added after VER2.3.0): Word was never run here before release, so every
' step says where it is (STAGE <name>) and every failure leaves
' item.<i>.code / errno / message / step. A hidden Word that shows a dialog
' blocks forever: Python then knows the step, reads the dialog's text from
' the Word process (WORDPID, found before any document is opened) and ends
' that Word.
' ============================================================================
Function IsWord(index)
    IsWord = (LCase(JobValue("kind." & index, "excel")) = "word")
End Function


' Preview of a Word document. When Word ends under it (462 / RPC errors),
' Word is started again and the preview is tried ONCE more, without the way
' it ended at: ended while writing the PDF -> page 1 as EMF; ended while
' drawing page 1 (EnhMetaFileBits) -> copy page 1.
'   way = "pdf"  : PDF, then EMF, then clipboard (word_pdf = 1)
'         "page" : EMF, then clipboard
'         "copy" : clipboard only
Function RunPreviewWord()
    Dim path, rc, lostAt, way

    RunPreviewWord = RC_ITEM
    path = JobValue("file.1", "")
    If Not gFso.FileExists(path) Then
        SetItemError 1, "FILE_NOT_FOUND", 0, path
        Exit Function
    End If
    CloseOpenCopy 1, path
    way = "page"
    If JobValue("word_pdf", "0") = "1" Then way = "pdf"
    rc = PreviewWordOnce(path, way)
    If rc <> RC_OK Then
        If WordWasLost() Then
            lostAt = gWordLostStage
            WriteResult "log", "Word ended during the preview (" & lostAt & "); start Word again and try once more"
            ClearItemError 1
            Emit "RETRY 1"
            If lostAt = "word.pdf" Then
                way = "page"
            ElseIf lostAt = "word.page" Then
                way = "copy"
            End If
            rc = PreviewWordOnce(path, way)
            If rc <> RC_OK Then
                If WordWasLost() Then SetWordGone 1
            End If
        End If
    End If
    RunPreviewWord = rc
End Function


' One try of the Word preview from the given way ("pdf" / "page" / "copy",
' see RunPreviewWord).
Function PreviewWordOnce(path, way)
    Dim doc, rng, cmd, attempt

    PreviewWordOnce = RC_ITEM
    gWordLostStage = ""
    gLastErrNumber = 0
    gLastErrDesc = ""
    If Not StartWord() Then
        WriteResult "fatal.code", "WORD_CREATE_FAILED"
        If Len(gLastErrDesc) > 0 Then WriteResult "fatal.message", gLastErrDesc
        SetItemError 1, "WORD_CREATE_FAILED", gLastErrNumber, gLastErrDesc
        PreviewWordOnce = RC_EXCEL
        Exit Function
    End If

    Set doc = OpenDoc(1, path)
    If doc Is Nothing Then Exit Function

    If way = "pdf" Then
        If EmitDocPdf(doc) Then
            Emit "PDFDONE 1"
            cmd = WaitCommand()
            If cmd = "OK" Then
                PreviewWordOnce = RC_OK
                CloseDoc doc
                Exit Function
            End If
            WriteResult "log", "Python did not take the PDF (" & cmd & "); draw page 1 instead"
        ElseIf Len(gWordLostStage) > 0 Then
            ' Word ended while writing the PDF: the page picture needs Word too
            SetItemError 1, "PREVIEW_FAILED", gLastErrNumber, gLastErrDesc
            CloseDoc doc
            Exit Function
        End If
        way = "page"
    End If

    WriteResult "item.1.sheet", "page 1 / " & PageCount(doc)
    WriteResult "item.1.range", "page 1"
    WriteResult "item.1.range_source", "word_page"
    cmd = "COPY"
    If way = "page" Then
        If EmitPageEmf(doc) Then
            Emit "EMFDONE 1"
            cmd = WaitCommand()
        ElseIf Len(gWordLostStage) > 0 Then
            ' Word ended while drawing the page: copying needs Word too
            SetItemError 1, "COPY_FAILED", gLastErrNumber, gLastErrDesc
            CloseDoc doc
            Exit Function
        End If
    Else
        WriteResult "log", "Word page picture skipped (Word ended at it in the first try)"
    End If
    If cmd = "OK" Then
        PreviewWordOnce = RC_OK
        CloseDoc doc
        Exit Function
    End If

    ' Fallback: copy page 1 to the clipboard
    WriteResult "log", "Word page picture not used; copy page 1 to the clipboard"
    Stage "word.copy"
    Set rng = FirstPageRange(doc)
    If rng Is Nothing Then
        SetItemError 1, "RANGE_FAILED", gLastErrNumber, gLastErrDesc
        CloseDoc doc
        Exit Function
    End If
    If Not CopyWordRange(rng) Then
        SetItemError 1, "COPY_FAILED", gLastErrNumber, gLastErrDesc
        CloseDoc doc
        Exit Function
    End If
    Emit "COPIED 1"
    cmd = WaitCommand()
    attempt = 1
    Do While cmd = "RECOPY" And attempt < 3
        attempt = attempt + 1
        WriteResult "log", "Clipboard replaced by another program; copy again (" & attempt & ")"
        If Not CopyWordRange(rng) Then
            SetItemError 1, "COPY_FAILED", gLastErrNumber, gLastErrDesc
            CloseDoc doc
            Exit Function
        End If
        Emit "COPIED 1"
        cmd = WaitCommand()
    Loop
    If cmd = "OK" Then
        PreviewWordOnce = RC_OK
    ElseIf cmd = "EXPORT" Then
        ' The chart export is an Excel feature; Word has nothing like it
        Emit "EXPORT_FAILED 1"
        SetItemError 1, "COPY_FAILED", gLastErrNumber, "No image on the clipboard (Word). " & gLastErrDesc
    End If
    CloseDoc doc
End Function


' The document as a PDF -> PDFDATA lines (VER2.4.0). Word writes the PDF to
' this script's own TEMP folder; it is read, sent and deleted at once.
' word_pdf_max_pages > 0: a longer document is cut to its first pages.
' ExportAsFixedFormat(OutputFileName, ExportFormat, OpenAfterExport,
'   OptimizeFor, Range, From, To, Item, IncludeDocProps, KeepIRM,
'   CreateBookmarks, DocStructureTags, BitmapMissingFonts, UseISO19005_1)
' OpenAfterExport = False: no PDF reader is started. On screen quality is
' enough for a preview (smaller, faster). Word 2007 without the add-in and
' Word 2003 have no PDF export: it fails and the caller draws page 1.
Function EmitDocPdf(doc)
    Dim outPath, pages, lastPage, number, description, sent

    EmitDocPdf = False
    sent = False
    On Error Resume Next
    pages = 0
    pages = CLng(PageCount(doc))
    lastPage = 0
    lastPage = CLng(JobValue("word_pdf_max_pages", "0"))
    Err.Clear
    outPath = NewTempPath(".pdf")
    If Len(outPath) = 0 Then
        WriteResult "log", "Word PDF: no temporary file name"
        Exit Function
    End If
    Stage "word.pdf"
    Err.Clear
    If lastPage > 0 And pages > lastPage Then
        doc.ExportAsFixedFormat outPath, wdExportFormatPDF, False, wdExportOptimizeForOnScreen, wdExportFromTo, 1, lastPage, wdExportDocumentContent, False, True, wdExportCreateNoBookmarks, False, True, False
    Else
        lastPage = pages
        doc.ExportAsFixedFormat outPath, wdExportFormatPDF, False, wdExportOptimizeForOnScreen, wdExportAllDocument, 1, 1, wdExportDocumentContent, False, True, wdExportCreateNoBookmarks, False, True, False
    End If
    number = Err.Number
    description = Err.Description
    Err.Clear
    If number <> 0 Then
        NoteError "Document.ExportAsFixedFormat (PDF)", number, description
        ' Word may have crashed inside the call: ask it
        If Len(gWordLostStage) = 0 Then
            If WordGoneNow() Then gWordLostStage = "word.pdf"
        End If
        DeleteTempFile outPath
        Exit Function
    End If
    If gFso.FileExists(outPath) Then
        If gFso.GetFile(outPath).Size > 0 Then sent = EmitFileBase64(outPath, "PDFDATA")
        Err.Clear
    End If
    DeleteTempFile outPath
    If Not sent Then
        WriteResult "log", "Word PDF: the file was not written or could not be read"
        Exit Function
    End If
    WriteResult "item.1.pages", CStr(pages)
    WriteResult "item.1.pdf_pages", CStr(lastPage)
    WriteResult "item.1.range_source", "word_pdf"
    EmitDocPdf = True
End Function


' This script's own folder for temporary files: %TEMP%\isp_preview (the TEMP
' of cscript, not of Python: see "NO FILES are exchanged with Python").
' Falls back to %TEMP% itself when the folder cannot be made.
Function TempFolder()
    Dim root, folder
    On Error Resume Next
    root = gFso.GetSpecialFolder(TemporaryFolder).Path
    folder = gFso.BuildPath(root, "isp_preview")
    If Not gFso.FolderExists(folder) Then gFso.CreateFolder folder
    Err.Clear
    If gFso.FolderExists(folder) Then
        TempFolder = folder
    Else
        TempFolder = root
    End If
End Function


' A new file name in this script's own temporary folder: isp_pv_<random><ext>.
' Several workers (two users, two editions) never get the same name: a name
' that is there already is not used.
Function NewTempPath(ext)
    Dim folder, attempt, candidate

    NewTempPath = ""
    On Error Resume Next
    folder = TempFolder()
    For attempt = 1 To 10
        candidate = gFso.BuildPath(folder, "isp_pv_" & Replace(gFso.GetTempName(), ".tmp", "") & ext)
        If Not gFso.FileExists(candidate) Then
            NewTempPath = candidate
            Exit Function
        End If
    Next
    LogErr "temporary file name"
End Function


' Delete a temporary file. Word may hold it a moment after writing: try again.
Sub DeleteTempFile(path)
    Dim attempt
    On Error Resume Next
    For attempt = 1 To 5
        If Not gFso.FileExists(path) Then Exit Sub
        Err.Clear
        gFso.DeleteFile path, True
        If Err.Number = 0 Then Exit Sub
        Err.Clear
        WScript.Sleep 200 * attempt
    Next
    WriteResult "log", "temporary file left (deleted when the worker starts next): " & path
    Err.Clear
End Sub


' When the worker starts: delete what an ended worker left in its temporary
' folder (isp_*.pdf / isp_*.emf older than an hour; a younger one may belong
' to a worker that is running now: another user, the other edition).
Sub SweepTempFiles()
    Dim folder, f, ext, removed
    removed = 0
    On Error Resume Next
    Set folder = gFso.GetFolder(TempFolder())
    If Err.Number <> 0 Then
        Err.Clear
        Exit Sub
    End If
    For Each f In folder.Files
        ext = LCase(gFso.GetExtensionName(f.Name))
        If LCase(Left(f.Name, 4)) = "isp_" And (ext = "pdf" Or ext = "emf") Then
            If DateDiff("n", f.DateLastModified, Now) > 60 Then
                f.Delete True
                If Err.Number = 0 Then removed = removed + 1
                Err.Clear
            End If
        End If
    Next
    Err.Clear
    If removed > 0 Then WriteResult "log", "Deleted " & removed & " temporary file(s) left by an ended worker"
End Sub


' Page 1 of the print layout as an EMF -> EMFDATA lines.
' The bytes go to base64 directly (MSXML); a temporary file is only the
' fallback. Each failing step is written to the log with its error.
Function EmitPageEmf(doc)
    Dim win, bits, size, stm, outPath

    EmitPageEmf = False
    Stage "word.page"
    On Error Resume Next
    Set win = Nothing
    Set win = doc.Windows(1)
    If Err.Number <> 0 Or win Is Nothing Then
        LogErr "Document.Windows(1)"
        Set win = Nothing
        Set win = doc.ActiveWindow
        LogErr "Document.ActiveWindow"
    End If
    If win Is Nothing Then
        WriteResult "log", "Word page picture: the document has no window"
        Exit Function
    End If
    Err.Clear
    win.View.Type = wdPrintView
    LogErr "Window.View.Type = print layout"
    doc.Repaginate
    LogErr "Document.Repaginate"
    bits = Empty
    bits = win.Panes(1).Pages(1).EnhMetaFileBits
    If Err.Number <> 0 Then
        LogErr "Pages(1).EnhMetaFileBits"
        ' Word may have crashed inside the call (0x80010105 and the like): ask it
        If Len(gWordLostStage) = 0 Then
            If WordGoneNow() Then gWordLostStage = "word.page"
        End If
        Exit Function
    End If
    size = -1
    size = UBound(bits) - LBound(bits) + 1
    Err.Clear
    WriteResult "log", "EnhMetaFileBits: " & size & " bytes (" & TypeName(bits) & ")"
    If size <= 0 Then Exit Function

    If EmitBytesBase64(bits, "EMFDATA") Then
        EmitPageEmf = True
        Exit Function
    End If

    ' Fallback: through a file in this script's own temporary folder
    outPath = NewTempPath(".emf")
    If Len(outPath) = 0 Then Exit Function
    Set stm = CreateObject("ADODB.Stream")
    stm.Type = adTypeBinary
    stm.Open
    stm.Write bits
    stm.SaveToFile outPath, adSaveCreateOverWrite
    stm.Close
    LogErr "EMF save (ADODB.Stream)"
    If gFso.FileExists(outPath) Then
        If gFso.GetFile(outPath).Size > 0 Then EmitPageEmf = EmitFileBase64(outPath, "EMFDATA")
        Err.Clear
    End If
    DeleteTempFile outPath
End Function


' From the top of the document to the top of page 2 (or the end)
Function FirstPageRange(doc)
    Dim p2, endPos, okFlag

    Set FirstPageRange = Nothing
    On Error Resume Next
    endPos = doc.Content.End
    Set p2 = Nothing
    Set p2 = doc.GoTo(wdGoToPage, wdGoToAbsolute, 2)
    okFlag = (Err.Number = 0)
    Err.Clear
    If okFlag Then
        If Not p2 Is Nothing Then
            If p2.Start > 0 And p2.Start < endPos Then endPos = p2.Start
        End If
    End If
    Err.Clear
    Set FirstPageRange = doc.Range(0, endPos)
    If Err.Number <> 0 Then
        LogErr "Document.Range"
        Set FirstPageRange = Nothing
    End If
End Function


Function CopyWordRange(rng)
    Dim attempt

    CopyWordRange = False
    On Error Resume Next
    For attempt = 1 To 5
        Err.Clear
        rng.CopyAsPicture
        If Err.Number <> 0 Then
            LogErr "Range.CopyAsPicture"
            rng.Copy
        End If
        If Err.Number = 0 Then
            CopyWordRange = True
            Exit Function
        End If
        LogErr "Range.Copy (retry " & attempt & ")"
        WScript.Sleep 300 * attempt
    Next
End Function


' One Word document of a print job. When Word ends under it before printing
' (462 / RPC errors), Word is started again and the document is tried ONCE
' more. Not when Word ended while printing: part of it may be on paper.
Function PrintWordItem(index, path, copies)
    Dim ok, lostAt

    ok = TryPrintWord(index, path, copies)
    If Not ok Then
        If WordWasLost() Then
            lostAt = gWordLostStage
            If lostAt = "word.print" Then
                WriteResult "log", "Word ended while printing; not printed again (it may be on paper)"
                SetWordGone index
            Else
                WriteResult "log", "Word ended (" & lostAt & "); start Word again and print once more"
                ClearItemError index
                Emit "RETRY " & index
                ok = TryPrintWord(index, path, copies)
                If Not ok Then
                    If WordWasLost() Then SetWordGone index
                End If
            End If
        End If
    End If
    PrintWordItem = ok
End Function


Function TryPrintWord(index, path, copies)
    TryPrintWord = False
    gWordLostStage = ""
    gLastErrNumber = 0
    gLastErrDesc = ""
    If StartWord() Then
        TryPrintWord = PrintOneWord(index, path, copies)
    Else
        SetItemError index, "WORD_CREATE_FAILED", gLastErrNumber, gLastErrDesc
    End If
End Function


Function PrintOneWord(index, path, copies)
    Dim doc, number, description

    PrintOneWord = False
    Set doc = OpenDoc(index, path)
    If doc Is Nothing Then Exit Function
    ' Nothing is asked of the hidden document before printing (VBA: Open,
    ' PrintOut, Close): not even the page count.
    WriteResult "item." & index & ".sheet", "Word"

    number = 0
    description = ""
    Stage "word.print"
    On Error Resume Next
    Err.Clear
    ' PrintOut(Background, Append, Range, OutputFileName, From, To, Item, Copies)
    ' Background = False: wait until Word has spooled it, then close
    doc.PrintOut False, , , , , , , copies
    number = Err.Number
    description = Err.Description
    Err.Clear
    On Error GoTo 0
    If number = 0 Then
        PrintOneWord = True
    ElseIf IsBusyError(number) Then
        SetItemError index, "WORD_BUSY", number, description
    Else
        SetItemError index, "PRINT_FAILED", number, description
    End If
    CloseDoc doc
End Function


Function PageCount(doc)
    PageCount = ""
    On Error Resume Next
    PageCount = CStr(doc.ComputeStatistics(wdStatisticPages))
    Err.Clear
End Function


Function StartWord()
    Dim number, description, before, pid, attempt, vis, docs

    StartWord = False
    On Error Resume Next
    If Not gWd Is Nothing Then
        If Not WordGoneNow() Then
            StartWord = True
            Exit Function
        End If
        LoseWord                ' the Word of the previous document has ended
    End If
    Stage "word.pid"
    Set before = WordProcessIds()
    Stage "word.create"
    Err.Clear
    Set gWd = CreateObject("Word.Application")
    If Err.Number <> 0 Then
        number = Err.Number
        description = Err.Description
        Err.Clear
        gLastErrNumber = number
        gLastErrDesc = description
        WriteResult "word.code", "WORD_CREATE_FAILED"
        WriteResult "word.errno", "0x" & Hex(number)
        WriteResult "word.message", description
        Set gWd = Nothing
        Exit Function
    End If
    If gWd Is Nothing Then
        gLastErrNumber = 0
        gLastErrDesc = "CreateObject(Word.Application) returned Nothing"
        Exit Function
    End If
    gWordReported = False
    ' The PID first, so that Python can always clean up - also when Word
    ' blocks on a dialog before any document window exists.
    Stage "word.pid"
    pid = NewWordProcessId(before)
    gWordPid = pid
    If pid > 0 Then Emit "WORDPID " & pid

    ' A Word that was already running (the user's) instead of a new one? Word
    ' normally starts a new WINWORD.EXE for CreateObject, but that is not
    ' promised. Such a Word is not hidden, its settings are given back at the
    ' end and it is never quit (FinishWord); Python never ends it (WORDSHARED).
    gWdShared = False
    If pid = 0 Then
        vis = False
        vis = gWd.Visible
        docs = 0
        docs = gWd.Documents.Count
        Err.Clear
        If vis Or docs > 0 Then gWdShared = True
    End If
    If gWdShared Then
        WriteResult "log", "CreateObject returned a Word that was already running (" & docs & _
                    " document(s), visible " & vis & "): not hidden, not quit"
        Emit "WORDSHARED 1"
        gSavedAlerts = gWd.DisplayAlerts
        Err.Clear
    End If

    ' Settings of this Word instance only (nothing that Word saves), the same
    ' two as the VBA version: DisplayAlerts and Visible. Nothing else is set
    ' (AutomationSecurity, WordBasic.DisableAutoMacros and FeatureInstall were
    ' set here up to VER2.4.0; Word did not print here while VBA could).
    ' A Word that has just started may reject calls for a moment: retry.
    Stage "word.setup"
    For attempt = 1 To 5
        Err.Clear
        gWd.DisplayAlerts = wdAlertsNone
        number = Err.Number
        description = Err.Description
        Err.Clear
        If number = 0 Then Exit For
        If Not IsBusyError(number) Then Exit For
        WScript.Sleep 500
    Next
    If number <> 0 Then NoteError "Word DisplayAlerts", number, description
    Err.Clear
    If Not gWdShared Then
        gWd.Visible = False
        LogErr "Word Visible"
    End If
    WriteResult "word.version", gWd.Version
    Err.Clear
    StartWord = True
End Function


' WINWORD.EXE processes that are running now ("pid" -> True). WMI is part
' of Windows; if it cannot be used, the list is empty.
Function WordProcessIds()
    Dim d, svc, procs, p
    Set d = CreateObject("Scripting.Dictionary")
    Set WordProcessIds = d
    On Error Resume Next
    Set svc = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
    If Err.Number <> 0 Then
        LogErr "WMI (before CreateObject)"
        Exit Function
    End If
    Set procs = svc.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name = 'WINWORD.EXE'")
    If Err.Number <> 0 Then
        LogErr "WMI query (before CreateObject)"
        Exit Function
    End If
    For Each p In procs
        d(CStr(p.ProcessId)) = True
    Next
    LogErr "WMI (WINWORD.EXE before CreateObject)"
End Function


' The Word that CreateObject has just started: a WINWORD.EXE that was not
' there before and was started for automation ("/Automation -Embedding").
' 0 = not sure (never guess: Python may end this process).
Function NewWordProcessId(before)
    Dim svc, procs, p, found, count, cmdLine

    NewWordProcessId = 0
    found = 0
    count = 0
    On Error Resume Next
    Set svc = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
    If Err.Number <> 0 Then
        LogErr "WMI (after CreateObject)"
        Exit Function
    End If
    Set procs = svc.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'WINWORD.EXE'")
    If Err.Number <> 0 Then
        LogErr "WMI query (after CreateObject)"
        Exit Function
    End If
    For Each p In procs
        If Not before.Exists(CStr(p.ProcessId)) Then
            cmdLine = ""
            cmdLine = LCase(p.CommandLine & "")
            If InStr(cmdLine, "embedding") > 0 Or InStr(cmdLine, "automation") > 0 Then
                found = p.ProcessId
                count = count + 1
            End If
        End If
    Next
    LogErr "WMI (WINWORD.EXE after CreateObject)"
    If count = 1 Then
        NewWordProcessId = found
    Else
        WriteResult "log", "Word PID not identified (" & count & " candidates)"
    End If
End Function


' Word no longer answers (it ended or crashed). A busy Word is not gone.
Function WordGoneNow()
    Dim v, attempt, number
    WordGoneNow = True
    If gWd Is Nothing Then Exit Function
    On Error Resume Next
    For attempt = 1 To 3
        Err.Clear
        v = ""
        v = gWd.Version
        number = Err.Number
        Err.Clear
        If number = 0 And Len(v) > 0 Then
            WordGoneNow = False
            Exit Function
        End If
        If Not IsBusyError(number) Then Exit Function
        WScript.Sleep 500
    Next
    WordGoneNow = False
End Function


' After a failed Word step: did Word end under us? Either a call failed with
' 462 / RPC errors (NoteGone kept the step) or Word no longer answers.
Function WordWasLost()
    WordWasLost = False
    If gWd Is Nothing Then Exit Function
    If WordGoneNow() Then
        If Len(gWordLostStage) = 0 Then gWordLostStage = gStage
        LoseWord
        WordWasLost = True
    ElseIf Len(gWordLostStage) > 0 Then
        WriteResult "log", "Word answers again after the connection was lost (" & gWordLostStage & ")"
        WordWasLost = True
    End If
End Function


' Word ended under us (crashed, closed by the user, or ended by someone else).
' The object is dropped; Python ends what is left of the Word this worker started.
Sub LoseWord()
    Dim where
    where = gWordLostStage
    If Len(where) = 0 Then where = gStage
    WriteResult "log", "Word is gone (at " & where & ")"
    Emit "WORDLOST " & where
    Set gWd = Nothing
    gWdShared = False
    gWordReported = False
    gWordPid = 0
End Sub


' The item failed twice because Word ended: WORD_GONE at the step it ended at.
Sub SetWordGone(index)
    Dim keep
    keep = gStage
    If Len(gWordLostStage) > 0 Then gStage = gWordLostStage
    SetItemError index, "WORD_GONE", gLastErrNumber, gLastErrDesc
    gStage = keep
End Sub


' Before a second try: the first try's error is not the answer any more.
Sub ClearItemError(index)
    WriteResult "item." & index & ".code", ""
    WriteResult "item." & index & ".errno", ""
    WriteResult "item." & index & ".message", ""
    WriteResult "item." & index & ".step", ""
End Sub


' End of a job: quit Word. If the user's own document opened in it, show Word
' and leave it running (never close what we did not open).
Sub FinishWord()
    Dim openDocs
    If gWd Is Nothing Then Exit Sub
    Stage "word.quit"
    On Error Resume Next
    openDocs = -1
    openDocs = gWd.Documents.Count
    If Err.Number <> 0 Then
        LogErr "Word Documents.Count (Word has gone)"
        Set gWd = Nothing
        Emit "WORDBYE 1"
        Exit Sub
    End If
    If gWdShared Then
        ' The user's Word: give its settings back, keep it as it is
        gWd.DisplayAlerts = gSavedAlerts
        Err.Clear
        WriteResult "log", "Word left running: it was running before this job (the user's Word)"
        Emit "WORDBYE 0"
    ElseIf openDocs = 0 Then
        gWd.NormalTemplate.Saved = True
        Err.Clear
        gWd.Quit wdDoNotSaveChanges
        LogErr "Word Quit"
        Emit "WORDBYE 1"
    Else
        gWd.Visible = True
        Err.Clear
        WriteResult "log", "Word left running: " & openDocs & " document(s) of the user are open"
        Emit "WORDBYE 0"
    End If
    Set gWd = Nothing
    gWdShared = False
    gWordReported = False
    gWordPid = 0
End Sub


' Documents.Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles,
'   PasswordDocument, PasswordTemplate, Revert, WritePasswordDocument,
'   WritePasswordTemplate, Format, Encoding, Visible)
' The same arguments as the VBA version (VER2.4.1): FileName, ReadOnly =
' True (no "File in Use" dialog, never written), AddToRecentFiles = False,
' Visible = False, plus ConfirmConversions = False (no "Convert File"
' dialog; nothing changes for a .doc / .docx). The others are left to Word,
' as in VBA (up to VER2.4.0 a dummy password, Revert and Format too).
' Arguments after Visible are left out on purpose: their order differs
' between Word versions.
Function OpenDoc(index, path)
    Dim doc, attempt, number, description

    Set OpenDoc = Nothing
    If Not gFso.FileExists(path) Then
        SetItemError index, "FILE_NOT_FOUND", 0, path
        Exit Function
    End If
    number = 0
    description = ""
    Stage "word.open"
    On Error Resume Next
    For attempt = 1 To 3
        Err.Clear
        Set doc = Nothing
        Set doc = gWd.Documents.Open(path, False, True, False, , , , , , , , False)
        number = Err.Number
        description = Err.Description
        Err.Clear
        If number = 0 Then Exit For
        WriteResult "log", "Documents.Open failed (" & attempt & "): 0x" & Hex(number) & " " & description
        If Not IsBusyError(number) Then Exit For
        WScript.Sleep 1000 * attempt
    Next
    If number = 0 Then
        If doc Is Nothing Then
            number = -1
            description = "Documents.Open returned Nothing"
        End If
    End If
    If number <> 0 Then
        If IsBusyError(number) Then
            SetItemError index, "WORD_BUSY", number, description
        Else
            SetItemError index, "OPEN_FAILED", number, description
        End If
        Exit Function
    End If
    Emit "OPENED " & index
    ReportWordWindow doc
    Set OpenDoc = doc
End Function


' Word has no Application.Hwnd; the document window has one (Word 2013+).
' Only when WMI could not tell the Word PID (WORDPID): the hidden document's
' window is not touched otherwise (VER2.4.1, the same as VBA).
Sub ReportWordWindow(doc)
    Dim hwnd
    If gWordReported Or gWdShared Then Exit Sub     ' the user's Word is not ours to track
    If gWordPid > 0 Then Exit Sub                   ' Python knows the PID already
    gWordReported = True
    On Error Resume Next
    hwnd = 0
    hwnd = doc.Windows(1).Hwnd
    If Err.Number <> 0 Or hwnd = 0 Then
        Err.Clear
        hwnd = 0
        hwnd = doc.ActiveWindow.Hwnd
    End If
    Err.Clear
    If hwnd <> 0 Then Emit "WORD " & CStr(hwnd)
End Sub


Sub CloseDoc(doc)
    On Error Resume Next
    If Not doc Is Nothing Then doc.Close wdDoNotSaveChanges
    Err.Clear
End Sub

' ----------------------------------------------------------------------------
' The same inspection sheet left open on this PC (the user's Excel / Word).
' The sheets are originals for printing only, so that one file is closed
' WITHOUT SAVING before it is opened here (job value close_open_copy = 1).
' Other workbooks / documents and the application itself are left alone.
' Only the Excel / Word that GetObject finds (the first one running) is
' looked at. A file open on another PC cannot be closed from here: it is
' opened read-only (no "File in Use" dialog) and prints anyway.
' ----------------------------------------------------------------------------
Sub CloseOpenCopy(index, path)
    If JobValue("close_open_copy", "0") <> "1" Then Exit Sub
    If Len(path) = 0 Then Exit Sub
    If IsWord(index) Then
        CloseOpenWordCopy index, path
    Else
        CloseOpenExcelCopy index, path
    End If
End Sub


Sub CloseOpenWordCopy(index, path)
    Dim app, d, i, count, name, same, closed

    Stage "word.close"
    closed = 0
    On Error Resume Next
    Set app = Nothing
    Set app = GetObject(, "Word.Application")
    If Err.Number <> 0 Then
        Err.Clear               ' no Word is running on this PC
        Exit Sub
    End If
    If app Is Nothing Then Exit Sub
    count = 0
    count = app.Documents.Count
    Err.Clear
    For i = count To 1 Step -1
        Set d = Nothing
        Set d = app.Documents(i)
        name = ""
        If Not d Is Nothing Then name = d.FullName
        Err.Clear
        ' (never test a call that can fail inside If: with Resume Next it runs Then)
        same = False
        same = SameFile(name, path)
        If same Then
            Err.Clear
            d.Close wdDoNotSaveChanges
            If Err.Number = 0 Then
                closed = closed + 1
            Else
                LogErr "Close the copy open in the user's Word"
            End If
        End If
    Next
    Err.Clear
    If closed > 0 Then
        WriteResult "log", "Closed the copy left open in the user's Word (not saved): " & path
        WriteResult "item." & index & ".closed_open", "Word"
    End If
End Sub


Sub CloseOpenExcelCopy(index, path)
    Dim app, wb, i, count, name, same, closed, mine

    Stage "excel.close"
    closed = 0
    On Error Resume Next
    Set app = Nothing
    Set app = GetObject(, "Excel.Application")
    If Err.Number <> 0 Then
        Err.Clear               ' no Excel is running on this PC
        Exit Sub
    End If
    If app Is Nothing Then Exit Sub
    ' The hidden Excel of this worker (kept between jobs) has nothing open now
    mine = False
    If Not gXl Is Nothing Then mine = (app.Hwnd = gXl.Hwnd)
    Err.Clear
    If mine Then Exit Sub
    count = 0
    count = app.Workbooks.Count
    Err.Clear
    For i = count To 1 Step -1
        Set wb = Nothing
        Set wb = app.Workbooks(i)
        name = ""
        If Not wb Is Nothing Then name = wb.FullName
        Err.Clear
        same = False
        same = SameFile(name, path)
        If same Then
            Err.Clear
            wb.Close False
            If Err.Number = 0 Then
                closed = closed + 1
            Else
                LogErr "Close the copy open in the user's Excel"
            End If
        End If
    Next
    Err.Clear
    If closed > 0 Then
        WriteResult "log", "Closed the copy left open in the user's Excel (not saved): " & path
        WriteResult "item." & index & ".closed_open", "Excel"
    End If
End Sub


' The same file: the same path, or (a mapped drive and the UNC path) the same
' name, size and time of the last change.
Function SameFile(a, b)
    Dim fa, fb, okFlag
    SameFile = False
    If Len(a) = 0 Or Len(b) = 0 Then Exit Function
    If LCase(a) = LCase(b) Then
        SameFile = True
        Exit Function
    End If
    On Error Resume Next
    If LCase(gFso.GetFileName(a)) <> LCase(gFso.GetFileName(b)) Then Exit Function
    Set fa = gFso.GetFile(a)
    Set fb = gFso.GetFile(b)
    okFlag = (Err.Number = 0)
    Err.Clear
    If Not okFlag Then Exit Function
    okFlag = False
    okFlag = (fa.Size = fb.Size And fa.DateLastModified = fb.DateLastModified)
    Err.Clear
    SameFile = okFlag
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
    Stage "excel.create"
    Err.Clear
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
    Stage "excel.open"
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


' The Word / Excel behind the object has ended: VBScript 462 "The remote server
' machine does not exist or is unavailable" (462 / 0x800A01CE),
' RPC_S_SERVER_UNAVAILABLE (0x800706BA), RPC_S_CALL_FAILED (0x800706BE),
' RPC_E_DISCONNECTED (0x80010108)
Function IsWordGoneError(number)
    IsWordGoneError = (number = 462 Or number = -2146827826 Or number = -2147023174 Or _
                       number = -2147023170 Or number = -2147417848)
End Function


' A Word call failed because Word is gone: keep the step it happened at.
Sub NoteGone(number)
    If Len(gWordLostStage) > 0 Then Exit Sub
    If Left(gStage, 5) <> "word." Then Exit Sub
    If IsWordGoneError(number) Then gWordLostStage = gStage
End Sub


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


' code + error number + description + the step it failed at (STAGE name)
Sub SetItemError(index, code, number, description)
    If number <> 0 Then
        gLastErrNumber = number
        gLastErrDesc = description
    End If
    NoteGone number
    WriteResult "item." & index & ".code", code
    If number <> 0 Then WriteResult "item." & index & ".errno", "0x" & Hex(number)
    If Len(description) > 0 Then WriteResult "item." & index & ".message", description
    If Len(gStage) > 0 Then WriteResult "item." & index & ".step", gStage
End Sub


' The step that starts now. Python keeps the last one: when Excel / Word
' stops answering (a hidden dialog), it can tell where.
Sub Stage(name)
    gStage = name
    Emit "STAGE " & name
End Sub


' Keep the current error (Err) as the last error and write it to the log.
' Call it right after the statement: any call to a procedure with
' On Error Resume Next clears Err.
Sub LogErr(where)
    Dim number, description
    number = Err.Number
    If number = 0 Then Exit Sub
    description = Err.Description
    Err.Clear
    NoteError where, number, description
End Sub


Sub NoteError(where, number, description)
    gLastErrNumber = number
    gLastErrDesc = description
    NoteGone number
    WriteResult "log", where & " failed: 0x" & Hex(number) & " " & description
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
