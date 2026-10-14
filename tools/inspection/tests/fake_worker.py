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
    """本物と同じく Word は Word の点検表が来たときだけ起動し、処理の終わりに終える。

    本物と同じく、起動したらすぐ(文書を開く前に)`WORDPID` を出す(fake_word_pid)。
    起こし直した2回目は fake_word_pid2。fake_user_word = 1 なら、CreateObject が利用者の
    Word を返した場合(本物は WORDSHARED を出し、WORDPID・窓を出さない)。
    """
    if STATE["word"]:
        return True
    emit("STAGE word.create")
    if job.get("fake_no_word") == "1":
        result("word.code", "WORD_CREATE_FAILED")
        result("word.errno", "0x800A01AD")
        return False
    STATE["word"] = True
    STATE["word_starts"] = STATE.get("word_starts", 0) + 1
    if job.get("fake_user_word") == "1":
        STATE["shared"] = True
        emit("WORDSHARED 1")
    else:
        pid = job.get("fake_word_pid2") if STATE["word_starts"] > 1 else job.get("fake_word_pid")
        if pid:
            emit(f"WORDPID {pid}")
    emit("STAGE word.setup")
    return True


def lose_word(stage):
    """本物の LoseWord: Word が途中で終わった(462)。"""
    result("log", f"Word is gone (at {stage})")
    emit(f"WORDLOST {stage}")
    STATE["word"] = False
    STATE["shared"] = False
    STATE["word_reported"] = False


def clear_item_error(i):
    for key in ("code", "errno", "message", "step"):
        result(f"item.{i}.{key}", "")


def gone_error(i, code, stage):
    """462(0x1CE)で失敗した、の結果(本物の SetItemError と同じ形)。"""
    result(f"item.{i}.code", code)
    result(f"item.{i}.errno", "0x1CE")
    result(f"item.{i}.message", "リモート サーバー マシンが存在しないか、利用できません。")
    result(f"item.{i}.step", stage)


def gone_at(job, attempt):
    """fake_word_gone = "<段階>"(1回目だけ)/ "<段階>:twice"(2回とも)。その回に終わる段階。"""
    spec = job.get("fake_word_gone", "")
    if not spec:
        return ""
    stage, _, times = spec.partition(":")
    return stage if attempt == 1 or times == "twice" else ""


def close_open_copies(job, indexes):
    """本物の CloseOpenCopy: close_open_copy = 1 のときだけ、この PC で開いたままのものを閉じる。

    fake_open_here = "1,3" … その番号の点検表が、この PC の Excel・Word で開いたままだった。
    """
    if job.get("close_open_copy") != "1":
        return
    here = {s for s in job.get("fake_open_here", "").split(",") if s}
    for i in indexes:
        app = "Word" if is_word(job, i) else "Excel"
        emit(f"STAGE {app.lower()}.close")
        if str(i) in here:
            result("log", f"Closed the copy left open in the user's {app} (not saved)")
            result(f"item.{i}.closed_open", app)


def open_doc(job, i):
    """本物の OpenDoc と同じ: STAGE word.open → 失敗なら item.<i>.code / errno / message / step。"""
    emit("STAGE word.open")
    if job.get("fake_word_hang") == "open":
        time.sleep(60)                                 # 隠れた確認の窓で止まった Word
    if job.get("fake_word_open_fail") == "1":
        result(f"item.{i}.code", "OPEN_FAILED")
        result(f"item.{i}.errno", "0x800A1520")
        result(f"item.{i}.message", "パスワードが正しくありません。")
        result(f"item.{i}.step", "word.open")
        return False
    emit(f"OPENED {i}")
    # 本物と同じく、文書の窓の hwnd は1回だけ。利用者の Word(WORDSHARED)の窓は出さない
    # (fake_shared_window = 1 なら、それでも出す: Python の側でも覚えないことを確かめる)
    shared_hides = STATE.get("shared") and job.get("fake_shared_window") != "1"
    if not STATE.get("word_reported") and not shared_hides:
        STATE["word_reported"] = True
        emit(f"WORD {job.get('fake_word_hwnd', '0')}")
    return True


def finish_word(job):
    if STATE["word"]:
        STATE["word"] = False
        STATE["word_reported"] = False
        emit("STAGE word.quit")
        user = job.get("fake_user_doc") == "1" or STATE.get("shared")
        STATE["shared"] = False
        emit("WORDBYE " + ("0" if user else "1"))


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
    if mode in ("print", "preview"):
        close_open_copies(job, range(1, (count if mode == "print" else 1) + 1))
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
            if is_word(job, i) and job.get("fake_word_gone"):
                ok = print_word_item(job, i)
                emit(f"END {i} {'OK' if ok else 'NG'}")
                rc = rc if ok else 4
            elif is_word(job, i) and not start_word(job):
                result(f"item.{i}.code", "WORD_CREATE_FAILED")
                emit(f"END {i} NG")
                rc = 4
            elif is_word(job, i) and not open_doc(job, i):
                emit(f"END {i} NG")
                rc = 4
            elif is_word(job, i) and job.get("fake_word_hang") == "print":
                emit("STAGE word.print")
                time.sleep(60)
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


def print_word_item(job, i):
    """本物の PrintWordItem: 文書を開く所で Word が終わったら起こし直して1回だけやり直す。
    印刷の途中で終わったら、紙に出ているかもしれないのでやり直さない。"""
    for attempt in (1, 2):
        start_word(job)
        stage = gone_at(job, attempt)
        if not stage:
            if open_doc(job, i):
                result(f"item.{i}.sheet", "pages 1")
                return True
            return False
        emit(f"STAGE word.{stage}")
        gone_error(i, "OPEN_FAILED" if stage == "open" else "PRINT_FAILED", f"word.{stage}")
        lose_word(f"word.{stage}")
        if stage == "print":
            result("log", "Word ended while printing; not printed again (it may be on paper)")
            gone_error(i, "WORD_GONE", "word.print")
            return False
        if attempt == 2:
            gone_error(i, "WORD_GONE", f"word.{stage}")
            return False
        clear_item_error(i)
        emit(f"RETRY {i}")
    return False


def preview_word(job):
    """本物の RunPreviewWord: Word が途中で終わったら起こし直して1回だけやり直す。
    1ページ目の絵で終わったなら、2回目はクリップボード経由。"""
    close_open_copies(job, [1])
    rc, stage = preview_word_once(job, 1, True)
    if rc == 0 or not stage:
        return rc
    clear_item_error(1)
    emit("RETRY 1")
    rc, stage = preview_word_once(job, 2, stage != "word.page")
    if rc != 0 and stage:
        gone_error(1, "WORD_GONE", stage)
    return rc


def preview_word_once(job, attempt, use_emf):
    """(終了コード, Word が終わった段階)。"""
    gone = gone_at(job, attempt)
    if gone == "open":
        start_word(job)
        emit("STAGE word.open")
        gone_error(1, "OPEN_FAILED", "word.open")
        lose_word("word.open")
        return 4, "word.open"
    rc = preview_word_body(job, gone == "page" or not use_emf, gone)
    if gone == "page" and rc != 0:
        lose_word("word.page")
        return rc, "word.page"
    return rc, ""


def preview_word_body(job, skip_emf=False, gone=""):
    """Word の1ページ目: EMFDATA → EMFDONE。COPY と言われたらクリップボード経由(COPIED)。"""
    if not start_word(job):
        result("fatal.code", "WORD_CREATE_FAILED")
        result("fatal.message", "ActiveX コンポーネントはオブジェクトを作成できません。")
        result("item.1.code", "WORD_CREATE_FAILED")
        result("item.1.errno", "0x800A01AD")
        result("item.1.message", "ActiveX コンポーネントはオブジェクトを作成できません。")
        result("item.1.step", "word.create")
        return 3
    if not open_doc(job, 1):
        return 4
    result("item.1.sheet", "page 1 / 2")
    result("item.1.range", "page 1")
    result("item.1.range_source", "word_page")
    if gone == "page":
        emit("STAGE word.page")                        # EnhMetaFileBits で Word が落ちた
        result("log", "Pages(1).EnhMetaFileBits failed: 0x800706BE")
        gone_error(1, "COPY_FAILED", "word.page")
        return 4
    if skip_emf:
        result("log", "Word page picture skipped (Word ended at it in the first try)")
    else:
        emit("STAGE word.page")
        emf = bytes.fromhex(job.get("fake_emf_hex", "0102030405"))
        b64 = base64.b64encode(emf).decode("ascii")
        for i in range(0, len(b64), 8):
            emit(f"EMFDATA {b64[i:i + 8]}")
        emit("EMFDONE 1")
        cmd = wait_command()
        if cmd == "OK":
            return 0
    result("log", "Word page picture not used; copy page 1 to the clipboard")
    emit("STAGE word.copy")
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
