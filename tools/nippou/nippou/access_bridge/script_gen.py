"""Dynamic VBScript generation for the Access bridge.

Per the architecture constraint (no ``pyodbc``/third-party DB drivers),
all actual .accdb I/O happens through OS-standard ``ADODB`` COM
automation, invoked from a VBScript file that Python writes to a temp
directory and runs via ``cscript.exe`` (see ``runner.py``). This mirrors
what the VBA side already did internally with ``adoConnection`` /
``adoSQL`` / ``ExecuteSQLTransaction`` -- we're simply moving the same
ADODB calls out of VBA and into a small generated script, since Python
itself cannot talk ADODB directly without a third-party bridge.

Every generated script follows the same contract on stdout so
``runner.py`` can parse it uniformly:

* success: single line ``OK`` (import scripts print ``OK:<n>`` where
  ``n`` is the row count written).
* failure: single line ``ERRCODE=<n>|ERRDESC=<text>`` followed by
  ``WScript.Quit 1``.
"""
from __future__ import annotations

CONNECTION_TEMPLATE = 'Provider=Microsoft.ACE.OLEDB.12.0;Data Source={path};'


def _vbs_str(value: str) -> str:
    """Quote ``value`` as a VBScript string literal."""
    return '"' + value.replace('"', '""') + '"'


def sql_literal(raw_value: str, category: str) -> str:
    """Port of ``BuildSQLLiteral``: turns a Python string into a literal
    safe to splice into an Access SQL statement.

    ``category`` is one of "NUMBER", "DATE", or anything else for TEXT
    (matches ``AdoTypeToCategory``'s vocabulary).
    """
    text = raw_value.strip() if raw_value is not None else ""
    if text == "" and category in ("NUMBER", "DATE"):
        return "NULL"
    if category == "NUMBER":
        return raw_value
    if category == "DATE":
        return f"#{raw_value}#"
    escaped = raw_value.replace("'", "''")
    return f"'{escaped}'"


def build_import_script(accdb_path: str, table_name: str, csv_out_path: str, sql_filter: str = "") -> str:
    """Generates a VBScript that SELECTs from ``table_name`` (optionally
    filtered) and writes the result as UTF-8 CSV to ``csv_out_path``.
    Ports the read side of ``GetRecordsArr`` / ``adoRecordset``."""
    where = f" WHERE {sql_filter}" if sql_filter else ""
    select_sql = f"SELECT * FROM [{table_name}]{where}"
    conn_str = CONNECTION_TEMPLATE.format(path=accdb_path)

    return f"""
Option Explicit
On Error Resume Next

Dim conn, rs, stream, i, line, val, rowCount
Set conn = CreateObject("ADODB.Connection")
conn.Open {_vbs_str(conn_str)}
If Err.Number <> 0 Then
    WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
    WScript.Quit 1
End If

Set rs = CreateObject("ADODB.Recordset")
rs.Open {_vbs_str(select_sql)}, conn, 0, 1 ' adOpenForwardOnly, adLockReadOnly
If Err.Number <> 0 Then
    WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
    conn.Close
    WScript.Quit 1
End If

Set stream = CreateObject("ADODB.Stream")
stream.Type = 2
stream.Charset = "utf-8"
stream.Open

line = ""
For i = 0 To rs.Fields.Count - 1
    If i > 0 Then line = line & ","
    line = line & CsvEscape(rs.Fields(i).Name)
Next
stream.WriteText line & vbCrLf

rowCount = 0
Do While Not rs.EOF
    line = ""
    For i = 0 To rs.Fields.Count - 1
        If i > 0 Then line = line & ","
        val = rs.Fields(i).Value
        If IsNull(val) Then
            line = line & ""
        Else
            line = line & CsvEscape(CStr(val))
        End If
    Next
    stream.WriteText line & vbCrLf
    rowCount = rowCount + 1
    rs.MoveNext
Loop

stream.SaveToFile {_vbs_str(csv_out_path)}, 2
stream.Close
rs.Close
conn.Close

If Err.Number <> 0 Then
    WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
    WScript.Quit 1
End If

WScript.StdOut.WriteLine "OK:" & rowCount
WScript.Quit 0

Function CsvEscape(s)
    Dim q
    q = Chr(34)
    If InStr(s, ",") > 0 Or InStr(s, q) > 0 Or InStr(s, vbCr) > 0 Or InStr(s, vbLf) > 0 Then
        CsvEscape = q & Replace(s, q, q & q) & q
    Else
        CsvEscape = s
    End If
End Function
"""


def build_push_script(accdb_path: str, statements: list[str]) -> str:
    """Generates a VBScript that executes ``statements`` inside a single
    ADODB transaction, mirroring ``ExecuteSQLTransaction`` /
    ``adoSQL``'s BeginTrans/CommitTrans/RollbackTrans pattern including
    lock-error identification via ``Err.Number``."""
    conn_str = CONNECTION_TEMPLATE.format(path=accdb_path)
    statement_lines = "\n".join(
        f"Statements.Add {_vbs_str(stmt)}" for stmt in statements
    )

    return f"""
Option Explicit

Dim conn, cmd, Statements, stmt, inTrans, execCount
Set Statements = CreateObject("System.Collections.ArrayList")
{statement_lines}

Set conn = CreateObject("ADODB.Connection")
inTrans = False

On Error Resume Next
conn.Open {_vbs_str(conn_str)}
If Err.Number <> 0 Then
    WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
    WScript.Quit 1
End If

conn.BeginTrans
inTrans = True
If Err.Number <> 0 Then
    WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
    WScript.Quit 1
End If

execCount = 0
For Each stmt In Statements
    conn.Execute stmt
    If Err.Number <> 0 Then
        conn.RollbackTrans
        WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
        conn.Close
        WScript.Quit 1
    End If
    execCount = execCount + 1
Next

conn.CommitTrans
If Err.Number <> 0 Then
    conn.RollbackTrans
    WScript.StdOut.WriteLine "ERRCODE=" & Err.Number & "|ERRDESC=" & Err.Description
    conn.Close
    WScript.Quit 1
End If

conn.Close
WScript.StdOut.WriteLine "OK:" & execCount
WScript.Quit 0
"""
