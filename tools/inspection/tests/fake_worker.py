"""excel_worker.vbs の代わりに、同じ手順(標準入出力の1行コマンド)で応答する偽物。

Windows / Excel が無い環境で、Python 側の通信処理(excel_service)をテストするために使う。
**ファイルは受け渡さない**(本物と同じ)。指示は標準入力の SET 行で届き、
動作は指示の fake_* キーで切り替える。
"""
import base64
import sys
import time


def hexw(text):
    """excel_worker.vbs の HexW と同じ(UTF-16 の1単位を4桁の16進)。"""
    data = str(text).encode("utf-16-be")
    return "".join(f"{data[i]:02X}{data[i + 1]:02X}" for i in range(0, len(data), 2))


def unhexw(text):
    """excel_worker.vbs の UnHexW と同じ。"""
    return bytes.fromhex(text).decode("utf-16-be")


def emit(text):
    sys.stdout.write(text + "\r\n")
    sys.stdout.flush()


STDIN_BROKEN = {"on": False}


def wait_command():
    if STDIN_BROKEN["on"]:
        return "EOF"
    line = sys.stdin.readline()
    return line.strip().upper() if line else "EOF"


def read_job():
    """SET … ENDJOB を1件ぶん読む。標準入力が閉じていれば None(もう仕事は無い)。"""
    job = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return job or None
        line = line.strip()
        if line == "ENDJOB":
            return job
        parts = line.split(" ")
        if len(parts) >= 2 and parts[0] == "SET":
            job[parts[1]] = unhexw(parts[2]) if len(parts) > 2 else ""


STATE = {"excel": False, "word": False, "jobs": 0}


def result(key, value):
    emit(f"RESULT {key} {hexw(value)}")


def start_excel(job):
    """本物と同じく、Excel は最初の1回だけ起動する(EXCEL 行もそのときだけ)。"""
    if job.get("fake_no_excel") == "1":
        result("fatal.code", "EXCEL_CREATE_FAILED")
        result("fatal.message", "ActiveX component can't create object")
        return False
    if STATE["excel"]:
        result("excel.reused", "1")
    else:
        STATE["excel"] = True
        emit("EXCEL 0")
    result("excel.version", "16.0")
    return True


def is_word(job, i):
    return job.get(f"kind.{i}", "excel") == "word"


def start_word(job):
    """本物と同じく Word は Word の点検表が来たときだけ起動し、処理の終わりに終える。"""
    if job.get("fake_no_word") == "1":
        result("word.code", "WORD_CREATE_FAILED")
        return False
    if not STATE["word"]:
        STATE["word"] = True
        emit("WORD 0")
    return True


def finish_word(job):
    if STATE["word"]:
        STATE["word"] = False
        emit("WORDBYE " + ("0" if job.get("fake_user_doc") == "1" else "1"))


def run(job):
    rc = run_job(job)
    finish_word(job)
    return rc


def run_job(job):
    mode = job.get("mode", "")
    STDIN_BROKEN["on"] = job.get("fake_stdin_eof") == "1"
    if mode == "preview" and is_word(job, 1):
        return preview_word(job)
    count = int(job.get("count", "0") or 0)
    all_word = mode == "print" and count > 0 and all(is_word(job, i) for i in range(1, count + 1))
    if not all_word and not start_excel(job):
        return 3
    rc = 0
    if mode == "print":
        count = int(job.get("count", "0") or 0)
        if count < 1:
            result("fatal.code", "JOB_READ_FAILED")
            result("fatal.message", "count=" + job.get("count", "(missing)"))
            return 2
        for i in range(1, count + 1):
            emit(f"NEXT {i}")
            cmd = wait_command()
            if cmd != "GO":
                if cmd == "EOF":
                    result("log", "StdIn.ReadLine failed: 0x1 test")
                result("stopped_at", i)
                result("stopped_by", cmd)
                break
            emit(f"BEGIN {i}")
            if job.get("fake_hang_at") == str(i):
                time.sleep(60)
            path = job.get(f"file.{i}", "")
            if is_word(job, i) and not start_word(job):
                result(f"item.{i}.code", "WORD_CREATE_FAILED")
                emit(f"END {i} NG")
                rc = 4
            elif "fail" in path:
                result(f"item.{i}.code", "PRINT_FAILED")
                result(f"item.{i}.message", "プリンターがオフラインです")
                emit(f"END {i} NG")
                rc = 4
            else:
                result(f"item.{i}.sheet", "点検シート")
                emit(f"END {i} OK")
    elif mode == "preview":
        if not job.get("file.1"):
            result("item.1.code", "FILE_NOT_FOUND")
            return 4
        if job.get("fake_copy_fail") == "1":
            result("item.1.code", "COPY_FAILED")
            result("item.1.errno", "0x800A03EC")
            result("item.1.message", "Range クラスの CopyPicture メソッドが失敗しました。")
            return 4
        result("item.1.sheet", "Sheet1")
        result("item.1.range", "A1:P40")
        result("item.1.range_source", "print_area")
        emit("COPIED 1")
        cmd = wait_command()
        attempt = 1
        while cmd == "RECOPY" and attempt < 3:             # 本物と同じく2回までコピーし直す
            attempt += 1
            result("log", f"Clipboard replaced by another program; copy again ({attempt})")
            emit("COPIED 1")
            cmd = wait_command()
        if cmd == "EXPORT":
            png = bytes.fromhex(job.get("fake_png_hex", ""))
            b64 = base64.b64encode(png).decode("ascii")
            for i in range(0, len(b64), 8):            # 本物は 3000 文字ずつ。分割を試すため細かく
                emit(f"PNGDATA {b64[i:i + 8]}")
            emit("EXPORTED 1")
    if job.get("fake_die_after") == "1":
        sys.exit(9)                                    # 途中で落ちた(FINISH を出さない)
    return rc


def preview_word(job):
    """Word の1ページ目: EMFDATA → EMFDONE。COPY と言われたらクリップボード経由(COPIED)。"""
    if not start_word(job):
        result("fatal.code", "WORD_CREATE_FAILED")
        return 3
    result("item.1.sheet", "page 1 / 2")
    result("item.1.range", "page 1")
    result("item.1.range_source", "word_page")
    emf = bytes.fromhex(job.get("fake_emf_hex", "0102030405"))
    b64 = base64.b64encode(emf).decode("ascii")
    for i in range(0, len(b64), 8):
        emit(f"EMFDATA {b64[i:i + 8]}")
    emit("EMFDONE 1")
    cmd = wait_command()
    if cmd == "OK":
        return 0
    result("log", "Word page picture not used; copy page 1 to the clipboard")
    emit("COPIED 1")
    cmd = wait_command()
    if cmd == "OK":
        return 0
    if cmd == "EXPORT":
        emit("EXPORT_FAILED 1")
        result("item.1.code", "COPY_FAILED")
        result("item.1.message", "No image on the clipboard (Word)")
    return 4


def main():
    while True:
        job = read_job()
        if job is None:
            break
        if job.get("fake_job_unreadable") == "1":
            job = {"mode": job.get("mode", "")}
        mode = job.get("mode", "")
        emit(f"READY {mode}")
        emit(f"JOB {len(job)}")
        rc = run(job)
        STATE["jobs"] += 1
        emit(f"FINISH {rc}")
    emit("BYE")


if __name__ == "__main__":
    main()
