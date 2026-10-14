"""Excel の操作（Python → cscript.exe → VBScript → Excel COM）。

Python 側に pywin32 等を入れられないため、Excel COM は VBScript
（app/vbscript/excel_worker.vbs）から操作する。Python と VBScript は
標準入出力の「1行コマンド」でやり取りする。

    Python → VBScript（標準入力・最初に）
    SET <key> <hex> …  ENDJOB        … 処理の指示(file.1 / copies など)

    VBScript → Python（標準出力・ASCII）      Python → VBScript（標準入力）
    READY <mode>
    EXCEL <hwnd>        … Excel の PID を記録
    COPIED 1            → OK / EXPORT       … プレビュー画像をコピーした
    EXPORTED 1 / EXPORT_FAILED 1
    NEXT <i>            → GO / STOP         … 印刷: 次の1件へ進むか
    BEGIN <i> / END <i> OK|NG
    FINISH <rc>
    JOB <n>             … 読めたジョブの項目数(0 = ジョブファイルを読めなかった)
    RESULT <key> <hex>  … 結果(item.1.code など)
    PNGDATA <base64>    … グラフ経由で書き出したプレビュー画像(クリップボードが使えないとき)
    WORD <hwnd>         … Word の PID を記録(Word の点検表を開いたとき)
    WORDPID <pid>       … 起動した Word の PID(文書を開く前。WMI で見つけたもの)
    STAGE <name>        … いまから始める段階(word.open など)。応答が無くなったとき
                          「どこで止まったか」を言うため
    WORDBYE 1|0         … Word を終えた / 利用者の文書が開いているので残した
    WORDSHARED 1        … CreateObject が利用者のすでに動いている Word を返した。その Word は
                          隠さず・終えず、こちらは**決して止めない**(PID を覚えない)
    WORDLOST <stage>    … Word が途中で終わった(VBScript 462・RPC のエラー)。残りを片付ける
    RETRY <i>           … Word を起こし直して、その点検表を**1回だけ**やり直す(VER2.3.2)
    EMFDATA <base64> …  EMFDONE 1  → OK / COPY
                        … Word の1ページ目(拡張メタファイル)。描けなければ COPY で
                          クリップボード経由(COPIED)に切り替えさせる

【Word の点検表(VER2.3.0)】
拡張子が .doc / .docx / .docm のものは、同じ処理プログラムが**非表示の Word** で開く
(`kind.<i>` = word を添える。決めるのはここの `kind_of`)。印刷は Document.PrintOut、
プレビューは1ページ目。Word は Excel と違って利用者がダブルクリックした文書を
こちらの Word で開いてしまうことがあるので、**処理のたびに終える**(利用者の文書が
開いていれば Word を見せて残す)。

【Word が途中で終わったとき(VER2.3.2)】
    印刷範囲の取得に失敗しました。リモート サーバー マシンが存在しないか、利用できません。 0x1CE
つかんでいた Word が終わっていた(落ちた・閉じられた)ときの VBScript のエラー 462。処理プログラムが
Word を起こし直して**1回だけ**やり直す(1ページ目の絵で落ちたなら、2回目はクリップボード経由)。
印刷の途中で落ちたときは、紙に出ているかもしれないのでやり直さない。2回とも駄目なら WORD_GONE。

【この PC で開いたままの点検表(VER2.3.2)】
点検表は原紙で印刷するだけなので、この PC の Excel・Word で**その点検表が**開いたままなら、処理
プログラムが保存せずに閉じてから開く(`close_open_copy`。ほかのブック・文書・アプリは触らない)。
ほかの PC で開かれているものは閉じられないが、読み取り専用で開くので印刷できる。共有フォルダの
所有者ファイル(`~$…`)から誰が開いているかを読んで知らせる(消さない)。

<hex> は UTF-16 の1単位を4桁の16進にしたもの。日本語を含む値(ファイルパス・
エラー内容)もこれで送るので、標準入出力は ASCII だけで済む(文字化けしない)。

**ファイルは一切受け渡さない。** Microsoft Store 版の Python は %LOCALAPPDATA% や
%TEMP% への書き込みを自分専用の控えへ振り替えるため、Python が書いたファイルが
cscript / Excel からは「パスが見つかりません」(0x4C)になる。ラインPCで実際に
起きた(VER1.1.0 まではジョブファイル・結果ファイルを使っていた)。

Excel プロセス管理:
    * 1回の処理 = Excel 起動 → Workbook を開く → 処理 → Close → Excel 終了
    * Application.Hwnd から Excel の PID と作成時刻を記録し、終了しなければ
      その PID だけを強制終了する（利用者が開いている Excel には触れない）
    * タイムアウト・異常時も必ず後処理する
"""
from __future__ import annotations

import base64
import hashlib
import binascii
import os
import queue
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from app.services import clipboard_image
from core import event_log
from core.process_tracking import (ProcessIdentity, TrackedProcessRegistry, get_process_identity,
                                   pid_from_hwnd, terminate_verified, wait_for_exit, window_texts)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER_VBS = os.path.join(APP_DIR, "vbscript", "excel_worker.vbs")
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
DUMMY_PASSWORD = "__isp_no_password__"

#: Word で開く拡張子(一覧に載せるかは設定の `extensions`。ここは「どちらで開くか」だけ)
WORD_EXTENSIONS = ("doc", "docx", "docm")


def kind_of(path: str) -> str:
    """その点検表を開くアプリ。Word の拡張子なら "word"、ほかは "excel"。"""
    ext = os.path.splitext(path or "")[1][1:].lower()
    return "word" if ext in WORD_EXTENSIONS else "excel"


def app_label(kinds: Any) -> str:
    """点検表の種類(kind_of の値の並び)→ 画面・記録に出すアプリの名前。

    Word だけなら "Word"、Excel だけ(・何も無い)なら "Excel"、混ざっていれば "Excel・Word"。
    """
    found = set(kinds or ())
    if found == {"word"}:
        return "Word"
    return "Excel・Word" if "word" in found else "Excel"

# 利用者に表示するメッセージ（要件定義書 16. エラー処理）
ERROR_MESSAGES: Dict[str, str] = {
    "FILE_NOT_FOUND": "対象の点検表ファイルが見つかりません。",
    "OPEN_FAILED": "点検表を開けませんでした。",
    "EXCEL_BUSY": "Excelが使用中です。しばらく待ってから再度お試しください。",
    "EXCEL_CREATE_FAILED": "Excelを起動できませんでした。Excelがインストールされているか確認してください。",
    "WORD_BUSY": "Wordが使用中です。しばらく待ってから再度お試しください。",
    "WORD_CREATE_FAILED": "Wordを起動できませんでした。Wordがインストールされているか確認してください。",
    "NO_VISIBLE_SHEET": "表示できるシートがありません。",
    "RANGE_FAILED": "印刷範囲の取得に失敗しました。",
    "COPY_FAILED": "プレビュー画像を取得できませんでした。",
    "PREVIEW_FAILED": "プレビュー画像を取得できませんでした。",
    "PRINT_FAILED": "印刷に失敗しました。",
    "TIMEOUT": "Excel・Wordが応答しませんでした。パスワード付きのファイルや、確認メッセージが表示される"
               "ファイルの可能性があります。",
    "WORKER_FAILED": "Excel処理プログラム（VBScript）の実行に失敗しました。",
    "JOB_READ_FAILED": "Excel処理プログラム（VBScript）が処理の指示を読み込めませんでした。",
    "STOPPED": "中止したため印刷していません。",
    "UNSUPPORTED": "この環境ではExcelを操作できません（Windows専用の機能です）。",
    "CLIPBOARD_BUSY": "ほかのアプリがクリップボードを使っていたため、プレビューを作れませんでした。"
                      "もう一度プレビューしてください。",
    "SUPERSEDED": "次の要求に切り替えたため、この要求は取りやめました。",
    "APP_UPDATED": "アプリが新しい版に入れ替えられました。いったん「終了」して、起動し直してください。",
    # VER2.3.2: つかんでいた Word が終わっていた(462)。起こし直して1回やり直しても駄目だったとき
    "WORD_GONE": "Word が途中で終わりました。この PC で Word を開いているなら閉じてから、もう一度押してください。",
}

#: Word の点検表のときの言い方(Excel の言葉のままだと、利用者が Excel を探してしまう)
WORD_MESSAGES: Dict[str, str] = {
    "WORKER_FAILED": "Word処理プログラム（VBScript）の実行に失敗しました。",
    "JOB_READ_FAILED": "Word処理プログラム（VBScript）が処理の指示を読み込めませんでした。",
    "UNSUPPORTED": "この環境ではWordを操作できません（Windows専用の機能です）。",
    "RANGE_FAILED": "Word の1ページ目を取得できませんでした。",
    "TIMEOUT": "Wordが応答しませんでした。パスワード付きの文書や、確認メッセージが表示される"
               "文書の可能性があります。",
}

#: 印刷の途中で Word が終わったとき。紙に出ているかもしれないので、やり直していない
WORD_GONE_PRINTING = ("Word が印刷の途中で終わりました。プリンターに出ていないか確かめてから、"
                      "もう一度押してください。この PC で Word を開いているなら閉じてください。")


def message_for(code: str, kind: str = "") -> str:
    """利用者に出す文。`kind`(kind_of の値)が word なら Word の言い方にする。"""
    if kind == "word" and code in WORD_MESSAGES:
        return WORD_MESSAGES[code]
    return ERROR_MESSAGES.get(code, "Excel・Wordの処理でエラーが発生しました。")


#: 処理プログラムの段階(`STAGE <name>`)→ 画面・記録に出す言葉。応答が無くなったとき
#: 「どこで止まったか」、失敗したとき「どこで失敗したか」(`item.<i>.step`)に使う
STAGE_LABELS: Dict[str, str] = {
    "excel.create": "Excel を起動する所",
    "excel.open": "Excel で点検表を開く所(Workbooks.Open)",
    "excel.copy": "Excel で範囲を画像にする所(CopyPicture)",
    "excel.export": "Excel のグラフ機能で画像を書き出す所",
    "excel.print": "Excel で印刷する所(PrintOut)",
    "word.pid": "起動した Word を探す所(WMI)",
    "word.create": "Word を起動する所(CreateObject)",
    "word.setup": "起動した Word の設定",
    "word.open": "Word で文書を開く所(Documents.Open)",
    "word.page": "Word の1ページ目を絵にする所(EnhMetaFileBits)",
    "word.copy": "Word の1ページ目をコピーする所",
    "word.print": "Word で印刷する所(PrintOut)",
    "word.quit": "Word を終える所(Quit)",
    "word.close": "この PC で開いたままの同じ文書を閉じる所",
    "excel.close": "この PC で開いたままの同じブックを閉じる所",
}


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage or "", stage or "")


#: よく出るエラー番号の手がかり。Word・Excel のエラーは 0x800A<番号> の形で届き、
#: VBScript 自身の実行時エラーは番号だけ(462 → "0x1CE")で届く(どちらも 0x800A<番号> で引く)。
#: 番号の意味が分かるものだけ添える
_ERRNO_HINTS: Dict[int, str] = {
    0x800A01CE: "VBScript 462: リモート サーバー マシンが存在しないか、利用できない ── つかんでいた "
                "Word / Excel が途中で終わった(落ちた・閉じられた・止められた)",
    0x800A01AD: "ActiveX コンポーネントを作成できない ── この PC に Word・Excel が入っていない・登録が壊れている",
    0x800A01A8: "オブジェクトが無い ── 処理プログラムの不具合の可能性",
    0x800A1520: "Word 5408: パスワードが正しくない ── パスワード付きの文書",
    0x800A1436: "Word 5174: ファイルが見つからない・開けない(パス・権限)",
    0x800A16A0: "Word 5792: ファイルが壊れている可能性",
    0x800A1066: "Word 4198: 命令が失敗した(印刷ではプリンター・ドライバーの問題が多い)",
    0x800A11FD: "Word 4605: この状態では使えない命令(文書の窓が無いなど)",
    0x800A1735: "Word 5941: 指定したもの(ページ・窓)が無い",
    0x80080005: "サーバーの実行に失敗 ── 止まった WINWORD.EXE / EXCEL.EXE が残っている可能性",
    0x800706BA: "RPC サーバーを利用できない ── Word / Excel が途中で終わった",
    0x80010108: "呼び出し先が切断された ── Word / Excel が途中で終わった",
    0x800706BE: "リモート プロシージャ コールに失敗 ── Word / Excel が命令の途中で落ちた",
    0x80010105: "呼び出し先が例外を出した ── Word / Excel が命令の途中で落ちた可能性",
    0x80010001: "呼び出しが拒否された ── Word / Excel が忙しい",
    0x8001010A: "呼び出し先が忙しい ── Word / Excel が別の処理中",
    0x80070005: "アクセスが拒否された(権限)",
}


def explain_errno(errno_text: str) -> str:
    """"0x800A1520" → "0x800A1520 = Word 5408: …"。分からない番号はそのまま。"""
    text = (errno_text or "").strip()
    try:
        number = int(text, 16) & 0xFFFFFFFF
    except ValueError:
        return text
    hint = _ERRNO_HINTS.get(number)
    if hint is None and number < 0x10000:
        hint = _ERRNO_HINTS.get(0x800A0000 | number)       # VBScript の番号だけ(0x1CE = 462)
    return f"{text} = {hint}" if hint else text


def _owner_name(data: bytes) -> str:
    """所有者ファイル(`~$…`)の中身 → 開いている人の名前(Office のユーザー名)。

    先頭1バイトが名前の長さ、続けて ANSI の名前。54 バイト目のあたりに同じ名前の
    UTF-16 がある(Word・Excel で位置が1つ前後する)。読めなければ空。
    """
    if len(data) < 2:
        return ""
    size = data[0]
    if size == 0:
        return ""
    for offset in (53, 54, 55, 56):
        end = offset + 2 + size * 2
        if len(data) >= end and data[offset] == size:
            try:
                text = data[offset + 2:end].decode("utf-16-le").strip()
            except UnicodeDecodeError:
                continue
            if text and text.isprintable():
                return text
    raw = data[1:1 + size]
    for encoding in ("mbcs", "cp932"):
        try:
            text = raw.decode(encoding).strip("\x00 ").strip()
        except (LookupError, UnicodeDecodeError):
            continue
        if text and text.isprintable():
            return text
    return ""


def owner_of(path: str) -> str:
    """ほかの人(ほかの PC)が開いている点検表なら、その人の名前。開かれていなければ空。

    Word・Excel は開いた文書の隣に所有者ファイルを作る(Excel は「~$」+名前、Word は名前の
    先頭1・2文字を「~$」に置き換えることがある)。**読むだけで、消さない**(相手が開いている
    あいだは相手のもの。開いたまま落ちた PC の残りでも、消すのは相手の Word に任せる)。
    """
    folder, name = os.path.split(path or "")
    if not name:
        return ""
    candidates = ["~$" + name]
    for cut in (1, 2):
        if len(name) > cut + 1:
            candidates.append("~$" + name[cut:])
    for candidate in candidates:
        try:
            with open(os.path.join(folder, candidate), "rb") as fp:
                data = fp.read(512)
        except OSError:
            continue
        who = _owner_name(data)
        if who:
            return who
    return ""


class ExcelError(Exception):
    def __init__(self, code: str, detail: str = "", message: Optional[str] = None, kind: str = ""):
        self.code = code
        self.detail = detail
        self.message = message or message_for(code, kind)
        super().__init__(f"{code}: {self.message} {detail}".strip())


@dataclass
class PreviewImage:
    data: bytes
    mime: str
    sheet: str = ""
    range_address: str = ""
    range_source: str = ""
    method: str = ""
    width: int = 0
    height: int = 0
    #: 知らせ(開いたままだったのを閉じた・ほかの人が開いている)。無ければ空
    notice: str = ""


@dataclass
class ItemOutcome:
    index: int
    ok: bool = False
    code: str = ""
    message: str = ""
    detail: str = ""
    sheet: str = ""
    processed: bool = False
    notice: str = ""


@dataclass
class JobOutcome:
    results: Dict[str, str]
    logs: List[str] = field(default_factory=list)
    finish_code: Optional[int] = None
    timed_out: bool = False
    stderr: str = ""
    elapsed_ms: int = 0
    #: 最後に始めた段階(`STAGE`)。応答が無くなったとき、どこで止まったか
    stage: str = ""
    #: 応答が無くなったとき Excel / Word が出していた窓(確認の窓の題名と文)
    windows: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """画面とログに出す「なぜ失敗したか」の手がかり(細部が何も無いとき用)。"""
        parts = []
        fatal = self.results.get("fatal.message") or self.results.get("fatal.code")
        if fatal:
            parts.append(fatal)
        if self.finish_code is not None:
            parts.append(f"VBScript 終了コード {self.finish_code}")
        elif self.stage:
            parts.append(f"止まった所: {stage_label(self.stage)}")
        if self.logs:
            parts.append(self.logs[-1])
        if self.stderr:
            parts.append(self.stderr[:300])
        return " / ".join(parts)

    def stuck_detail(self) -> str:
        """応答が無くなったときの手がかり: 止まった所と、そのとき出ていた窓。"""
        parts = []
        if self.stage:
            parts.append(f"止まった所: {stage_label(self.stage)}")
        if self.windows:
            parts.append("出ていた窓: " + " ／ ".join(self.windows))
        elif self.timed_out:
            parts.append("出ていた窓: (見つからず)")
        return " / ".join(parts)


def decode_hex_utf16(text: str) -> str:  # noqa: D401
    """VBScript の HexW で送られた値を戻す(4桁 = UTF-16 の1単位)。"""
    try:
        return bytes.fromhex(text).decode("utf-16-be", errors="replace")
    except ValueError:
        return text


# ======================================================================
# ジョブファイル・結果ファイル
# ======================================================================
def encode_hex_utf16(value: Any) -> str:
    """excel_worker.vbs の UnHexW で戻せる形(UTF-16 の1単位を4桁の16進)。

    改行は値に入れない(1行1項目のため)。
    """
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    data = text.encode("utf-16-be")
    return data.hex().upper()


# ======================================================================
# cscript プロセス
# ======================================================================
class _WorkerProcess:
    def __init__(self, cmd: List[str], cwd: str):
        self.cmd = cmd
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr: List[str] = []

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def start(self) -> None:
        flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=flags)
        threading.Thread(target=self._pump_stdout, name="vbs-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name="vbs-stderr", daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)
            _close_quietly(self.proc.stdout)

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            data = self.proc.stderr.read()
        except (OSError, ValueError):
            data = b""
        finally:
            _close_quietly(self.proc.stderr)
        for encoding in ("oem", "mbcs", "utf-8"):
            try:
                self._stderr.append(data.decode(encoding, errors="replace"))
                break
            except LookupError:
                continue

    def next_line(self, timeout: float) -> Optional[str]:
        """次の1行。None は終了（EOF）。timeout 秒応答が無ければ TimeoutError。"""
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError()

    def send(self, text: str) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write((text + "\r\n").encode("ascii"))
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close_stdin(self) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def wait(self, timeout: float) -> Optional[int]:
        if not self.proc:
            return None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr).strip()


class _Session:
    """使い回している cscript(excel_worker.vbs)と、その中の Excel 1つ。"""

    def __init__(self, worker: _WorkerProcess, cscript: Optional[ProcessIdentity]):
        self.worker = worker
        self.cscript = cscript
        self.excel: Optional[ProcessIdentity] = None
        # Word は処理のたびに終える(`WORDBYE`)。いま開いている Word だけ
        self.word: Optional[ProcessIdentity] = None
        # いまの Word は利用者がすでに開いていたもの(`WORDSHARED`)。PID を覚えない・止めない
        self.word_shared = False
        self.jobs = 0
        self.printer = ""


# ======================================================================
# VBScript 経由の実装（Windows）
# ======================================================================
class VbsExcelBackend:
    name = "vbscript"

    def __init__(self, cfg: Any, work_dir: str, registry: Any, logger: Any):
        self.cfg = cfg
        self.excel_cfg = cfg.excel
        self.work_dir = work_dir
        self.registry = registry
        self.log = logger
        os.makedirs(work_dir, exist_ok=True)
        self.keep_alive_sec = int(getattr(self.excel_cfg, "keep_alive_sec", 120) or 0)
        # 既定のプリンター(Excel は起動したときのプリンターへ印刷する)
        from app.services.system_info import default_printer
        self.printer_provider = default_printer
        # 起動したときの excel_worker.vbs。共有フォルダのアプリが途中で入れ替えられたら
        # 古い Python と新しい VBScript を組み合わせない
        self._worker_digest = _file_digest(WORKER_VBS)
        self._session: Optional[_Session] = None
        self._session_lock = threading.RLock()
        self._idle_timer: Optional[threading.Timer] = None
        self.cscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cscript.exe")
        if not os.path.isfile(self.cscript):
            self.cscript = shutil.which("cscript") or self.cscript

    # ---- 共通設定 -------------------------------------------------
    def _base_params(self) -> Dict[str, Any]:
        e = self.excel_cfg
        return {
            "disable_macros": 1 if e.disable_macros else 0,
            "open_password": DUMMY_PASSWORD if e.block_password_prompt else "",
            # この PC で開いたままのその点検表を、保存せずに閉じてから開く(原紙・印刷だけ)
            "close_open_copy": 1 if getattr(e, "close_open_copy", True) else 0,
        }

    def _notice(self, results: Dict[str, str], index: int, path: str, owner: str, verb: str) -> str:
        """開いたままだったのを閉じた / ほかの人が開いている、の知らせ(記録にも残す)。"""
        name = os.path.splitext(os.path.basename(path))[0]
        closed = results.get(f"item.{index}.closed_open", "")
        if closed:
            self.log.info("この PC の %s で開いたままだった点検表を、保存せずに閉じました: %s", closed, path)
            event_log.record(f"{'word' if closed == 'Word' else 'excel'}.close_open", event_log.INFO,
                             file=path, app=closed)
            return f"開いたままだった「{name}」を閉じてから{verb}しました(この PC の {closed}。保存はしていません)"
        if owner:
            self.log.info("ほかの人が開いている点検表(読み取り専用で開きました): %s 開いている人=%s", path, owner)
            event_log.record("file.in_use", event_log.INFO, file=path, owner=owner, app=app_label([kind_of(path)]))
            return f"{owner}さんが「{name}」を開いたままです(印刷はできます)"
        return ""

    # ---- プレビュー -----------------------------------------------
    def preview(self, path: str) -> PreviewImage:
        e = self.excel_cfg
        kind = kind_of(path)
        word = kind == "word"
        owner = owner_of(path)          # ほかの PC が開いているか(所有者ファイルを読むだけ)
        params = {
            **self._base_params(),
            "count": 1,
            "file.1": path,
            "kind.1": "word" if word else "excel",
            "appearance": 2 if e.preview_appearance == "printer" else 1,
            "max_rows": e.preview_max_rows,
            "max_cols": e.preview_max_cols,
        }
        scale = e.preview_scale
        max_pixels = e.preview_max_pixels
        seq_before = clipboard_image.sequence_number()
        state: Dict[str, Any] = {"png": None, "info": {}, "clip_error": "", "exported": None,
                                 "chunks": [], "emf": [], "recopies": 0, "owner_error": False,
                                 "method": ""}

        def handler(cmd: str, args: List[str], worker: _WorkerProcess) -> None:
            if cmd == "RETRY":
                # Word を起こし直して2回目。1回目の書きかけは捨てる
                state.update(emf=[], chunks=[], exported=None, recopies=0, clip_error="", owner_error=False)
            elif cmd == "EMFDATA" and args:
                state["emf"].append(args[0])
            elif cmd == "EMFDONE":
                # Word の1ページ目。描けなければクリップボード経由に切り替えさせる
                try:
                    data = base64.b64decode("".join(state["emf"]))
                    png, info = clipboard_image.emf_to_png(data, scale, max_pixels)
                    state.update(png=png, info=info, method="word-page")
                    worker.send("OK")
                except (ValueError, binascii.Error, clipboard_image.ClipboardImageError) as exc:
                    state["clip_error"] = str(exc)
                    self.log.warning("Word の1ページ目を描けないため、クリップボード経由にします: %s", exc)
                    worker.send("COPY")
                state["emf"] = []
            elif cmd == "COPIED":
                # 置いたのはこちらの Excel / Word か(Word の点検表なら Word)
                session = self._session
                owner = (session.word if word else session.excel) if session else None
                try:
                    png, info = clipboard_image.read_image_png(
                        scale, max_pixels, previous_sequence=seq_before,
                        expected_owner=owner.pid if owner else None)
                    state.update(png=png, info=info)
                    worker.send("OK")
                except clipboard_image.ClipboardOwnerError as exc:
                    # 別のものを点検表として出さない。コピーし直させる(グラフ経由も
                    # クリップボードを使うので、そちらへは逃がさない)
                    self.log.warning("プレビュー: %s", exc)
                    if state["recopies"] < 2:
                        state["recopies"] += 1
                        worker.send("RECOPY")
                    else:
                        state.update(clip_error=str(exc), owner_error=True)
                        worker.send("SKIP")
                except clipboard_image.ClipboardImageError as exc:
                    state["clip_error"] = str(exc)
                    self.log.warning("クリップボードから画像を取得できないため、Excel のグラフ機能で"
                                     "画像を書き出します: %s", exc)
                    worker.send("EXPORT")
            elif cmd == "PNGDATA" and args:
                state["chunks"].append(args[0])
            elif cmd == "EXPORTED":
                state["exported"] = True
            elif cmd == "EXPORT_FAILED":
                state["exported"] = False

        outcome = self._run_job("preview", params, handler, e.preview_timeout_sec)
        res = outcome.results
        meta = dict(sheet=res.get("item.1.sheet", ""), range_address=res.get("item.1.range", ""),
                    range_source=res.get("item.1.range_source", ""),
                    notice=self._notice(res, 1, path, owner, "プレビュー"))
        if state["png"]:
            # 画像取得後に Excel の終了で止まった場合も、画像は使える（Excel は強制終了済み）
            info = state["info"]
            return PreviewImage(state["png"], "image/png",
                                method=state["method"] or f"clipboard-{info.get('format')}",
                                width=info.get("width", 0), height=info.get("height", 0), **meta)
        if state["exported"] and state["chunks"]:
            try:
                data = base64.b64decode("".join(state["chunks"]))
            except (ValueError, binascii.Error) as exc:
                raise ExcelError("PREVIEW_FAILED", f"書き出した画像を読めませんでした: {exc}", kind=kind)
            return PreviewImage(data, "image/png", method="chart-export", **meta)
        if outcome.timed_out:
            raise ExcelError("TIMEOUT", " / ".join(
                p for p in (outcome.stuck_detail(), self._detail(res, 1)) if p), kind=kind)
        if state["owner_error"]:
            raise ExcelError("CLIPBOARD_BUSY", state["clip_error"], kind=kind)
        if res.get("fatal.code") == "JOB_READ_FAILED":
            # 指示が届いていなければ、ほかの断り(「ファイルが見つかりません」)は結果にすぎない
            raise ExcelError("JOB_READ_FAILED", outcome.summary(), kind=kind)
        code = res.get("item.1.code") or res.get("fatal.code") or ("WORKER_FAILED" if outcome.finish_code is None
                                                                    else "PREVIEW_FAILED")
        detail = self._detail(res, 1) or state["clip_error"] or outcome.summary()
        raise ExcelError(code, detail, kind=kind)

    # ---- 印刷 -----------------------------------------------------
    def print_files(self, files: List[str], copies: int, on_event: Callable[..., None],
                    should_continue: Callable[[], bool]) -> List[ItemOutcome]:
        params: Dict[str, Any] = {**self._base_params(), "count": len(files), "copies": copies}
        for index, path in enumerate(files, 1):
            params[f"file.{index}"] = path
            params[f"kind.{index}"] = kind_of(path)
        owners = {i: owner_of(path) for i, path in enumerate(files, 1)}
        outcomes = {i: ItemOutcome(i) for i in range(1, len(files) + 1)}
        current = {"index": 0, "stop_sent": False}

        def handler(cmd: str, args: List[str], worker: _WorkerProcess) -> None:
            index = _int(args[0]) if args else 0
            if cmd == "NEXT":
                go = should_continue()
                current["stop_sent"] = current["stop_sent"] or not go
                worker.send("GO" if go else "STOP")
            elif cmd == "BEGIN" and index in outcomes:
                current["index"] = index
                on_event("begin", index)
            elif cmd == "END" and index in outcomes:
                outcomes[index].processed = True
                outcomes[index].ok = len(args) > 1 and args[1].upper() == "OK"
                on_event("end", index, outcomes[index].ok)

        outcome = self._run_job("print", params, handler, self.excel_cfg.print_item_timeout_sec)
        res = outcome.results
        for index, item in outcomes.items():
            item.sheet = res.get(f"item.{index}.sheet", "")
            kind = kind_of(files[index - 1])
            if item.processed or res.get(f"item.{index}.closed_open"):
                item.notice = self._notice(res, index, files[index - 1], owners[index], "印刷")
            if item.ok:
                continue
            if not item.processed:
                if outcome.timed_out and index == current["index"]:
                    item.code = "TIMEOUT"
                elif res.get("fatal.code") and not any(o.processed for o in outcomes.values()):
                    item.code = res["fatal.code"]
                elif current["stop_sent"] or outcome.timed_out:
                    # **中止と言えるのは、こちらが止めたときだけ。** 以前は処理されなかった
                    # ものを全部「中止したため」にしていて、原因が画面から消えていた
                    item.code = "STOPPED"
                else:
                    item.code = "WORKER_FAILED"
                item.detail = self._detail(res, index) or (
                    "" if item.code == "STOPPED" else outcome.summary())
                if item.code == "TIMEOUT":
                    item.detail = " / ".join(p for p in (outcome.stuck_detail(), item.detail) if p)
            else:
                item.code = res.get(f"item.{index}.code") or "PRINT_FAILED"
                item.detail = self._detail(res, index)
            item.message = message_for(item.code, kind)
            if item.code == "WORD_GONE" and res.get(f"item.{index}.step") == "word.print":
                item.message = WORD_GONE_PRINTING
        return [outcomes[i] for i in sorted(outcomes)]

    # ---- 動作確認 -------------------------------------------------
    def selftest(self) -> Dict[str, Any]:
        started = time.perf_counter()
        outcome = self._run_job("selftest", self._base_params(), lambda *a: None, 60)
        res = outcome.results
        ok = outcome.finish_code == 0 and not outcome.timed_out
        return {"ok": ok, "version": res.get("excel.version", ""),
                "code": res.get("fatal.code", "") if not ok else "",
                "detail": res.get("fatal.message", "") or outcome.stderr,
                "seconds": round(time.perf_counter() - started, 1)}

    # ---- ジョブ実行 -----------------------------------------------
    # **Excel は続けて使い回す。** Excel の起動と終了はそれぞれ数秒かかるので、
    # 1回ごとに起動していたころはプレビュー1枚・印刷1回のたびに待たされていた
    # (ラインPCで「起動も印刷もやや重い」)。処理が終わっても `keep_alive_sec`
    # のあいだは残し、次の処理はすぐに始める。使われなければ閉じる。
    def _command(self) -> List[str]:
        return [self.cscript, "//nologo", "//E:vbscript", WORKER_VBS, "session"]

    def _open_session(self) -> "_Session":
        if _file_digest(WORKER_VBS) != self._worker_digest:
            raise ExcelError("APP_UPDATED", f"{WORKER_VBS} が起動後に変わりました")
        # 作業フォルダも AppData を避ける(Store 版 Python が作ったフォルダは cscript から見えない)
        worker = _WorkerProcess(self._command(), os.path.dirname(WORKER_VBS))
        try:
            worker.start()
        except OSError as exc:
            raise ExcelError("WORKER_FAILED", f"cscript.exe を起動できません: {exc}")
        session = _Session(worker, get_process_identity(worker.pid or 0))
        session.printer = self._current_printer()
        if session.cscript:
            self.registry.add(session.cscript, "cscript:session")
        self.log.info("Excel処理プログラムを起動しました pid=%s", worker.pid)
        return session

    def _run_job(self, mode: str, params: Dict[str, Any],
                 handler: Callable[[str, List[str], _WorkerProcess], None], idle_timeout: int) -> JobOutcome:
        if not os.path.isfile(WORKER_VBS):
            raise ExcelError("WORKER_FAILED", f"{WORKER_VBS} が見つかりません")
        with self._session_lock:
            self._cancel_idle_timer()
            return self._run_job_locked(mode, params, handler, idle_timeout)

    def _run_job_locked(self, mode: str, params: Dict[str, Any],
                        handler: Callable[[str, List[str], _WorkerProcess], None],
                        idle_timeout: int) -> JobOutcome:
        started = time.perf_counter()
        job = {"mode": mode, **params}
        session = self._session
        if session is not None and not session.worker.alive():
            self._close_session(session, force=True, reason="処理プログラムが終わっていた")
            session = None
        if session is not None and session.printer != self._current_printer():
            # **Excel は起動したときのプリンターへ印刷する。** 使い回している間に既定の
            # プリンターを替えると(1台で複数ラインを受け持つ端末)、前のラインの
            # プリンターへ出てしまう。替わっていたら Excel を起動し直す
            self._close_session(session, force=False, reason="既定のプリンターが替わった")
            session = None
        if session is None:
            session = self._session = self._open_session()
        reused = session.jobs > 0
        session.jobs += 1
        session.word_shared = False
        app = app_label(v for k, v in job.items() if k.startswith("kind."))
        worker = session.worker
        finish_code: Optional[int] = None
        timed_out = False
        ended = False
        job_keys: Optional[int] = None
        results: Dict[str, str] = {}
        logs: List[str] = []
        stage = ""
        self.log.info("Excel処理開始 mode=%s 件数=%s %s", mode, params.get("count", "-"),
                      "(起動済みの Excel を使用)" if reused else "")
        for key, value in job.items():
            worker.send(f"SET {key} {encode_hex_utf16(value)}")
        worker.send("ENDJOB")
        while True:
            try:
                line = worker.next_line(idle_timeout)
            except TimeoutError:
                timed_out = True
                self.log.error("Excelが %s 秒応答しないため処理を中断します mode=%s", idle_timeout, mode)
                break
            if line is None:
                ended = True
                break
            parts = line.split()
            command = parts[0].upper()
            if command == "EXCEL" and len(parts) > 1:
                excel = self._track_excel(parts[1])
                if session.excel and excel and session.excel.pid != excel.pid:
                    self.registry.remove(session.excel.pid)     # 前の Excel は終わっていた
                session.excel = excel or session.excel
            elif command == "STAGE":
                stage = parts[1] if len(parts) > 1 else ""
            elif command == "WORDSHARED":
                self._word_shared(session)
            elif command == "WORDPID" and len(parts) > 1:
                if not session.word_shared:
                    self._adopt_word(session, self._identify_word(_int(parts[1])))
            elif command == "WORD" and len(parts) > 1:
                # 利用者の Word(WORDSHARED)の窓は覚えない。覚えると後始末で止めてしまう
                if not session.word_shared and not (
                        session.word and pid_from_hwnd(_int(parts[1])) == session.word.pid):
                    self._adopt_word(session, self._track_app(parts[1], "word"))
            elif command == "WORDBYE":
                self._word_closed(session, quit_=len(parts) < 2 or parts[1] != "0")
                session.word_shared = False
            elif command == "WORDLOST":
                self._word_lost(session, parts[1] if len(parts) > 1 else stage)
            elif command == "RETRY":
                self.log.warning("Word が途中で終わったため、起こし直して %s 件目をもう一度やります",
                                 parts[1] if len(parts) > 1 else "?")
                handler(command, parts[1:], worker)
            elif command == "FINISH":
                finish_code = _int(parts[1]) if len(parts) > 1 else 0
                stage = ""
                break
            elif command == "RESULT" and len(parts) > 1:
                value = decode_hex_utf16(parts[2]) if len(parts) > 2 else ""
                if parts[1] == "log":
                    logs.append(value)
                else:
                    results[parts[1]] = value
            elif command == "JOB":
                job_keys = _int(parts[1]) if len(parts) > 1 else 0
            elif command in ("READY", "OPENED", "BYE"):
                self.log.debug("worker: %s", line)
            else:
                handler(command, parts[1:], worker)

        expected = len(job)
        if job_keys is not None and job_keys < expected and "fatal.code" not in results:
            results["fatal.code"] = "JOB_READ_FAILED"
            results["fatal.message"] = f"処理の指示が {job_keys}/{expected} 項目しか届きませんでした"
        broken = timed_out or ended or finish_code is None or results.get("fatal.code") == "JOB_READ_FAILED"
        stderr = ""
        windows: List[str] = []
        word_pid = session.word.pid if session.word else None     # 後始末で消える前に
        if timed_out:
            # 止める前に、Excel / Word が出していた窓(隠れた確認の窓)を読んでおく
            windows = self._windows_of(session)
            self.log.error("止まった所: %s 出ていた窓: %s", stage_label(stage) or "(不明)", windows or "(見つからず)")
        if broken:
            # 途中で止まった・応答が無い処理プログラムは使い回さない
            self._close_session(session, force=timed_out, reason="処理の途中で止まった")
            stderr = worker.stderr_text
        elif self.keep_alive_sec <= 0:
            self._close_session(session, force=False, reason="使い回さない設定")
        else:
            self._schedule_idle_close()

        for entry in logs:
            self.log.debug("worker log: %s", entry)
        outcome = JobOutcome(results, logs, finish_code, timed_out, stderr,
                             int((time.perf_counter() - started) * 1000), stage=stage, windows=windows)
        if timed_out or finish_code not in (0, None) or stderr or finish_code is None:
            self.log.warning("Excel処理結果 mode=%s finish=%s timeout=%s 結果=%s 指示=%s stderr=%s",
                             mode, finish_code, timed_out, results, job, stderr[:500])
            # なぜなぜ用: 処理プログラム(VBScript)と Excel が何を返したかを丸ごと残す。
            # **途中で終わった(WORKER_FAILED)と言うのは FINISH が来なかったときだけ。** 以前は
            # 点検表1件の失敗(終了コード 4)まで「処理プログラムが途中で終わった」と記録していて、
            # ログの「なぜ」が本当の原因(0x1CE = Word が終わっていた)からずれていた
            code, message = self._job_failure(results, timed_out, finish_code, app)
            event_log.record(
                "excel.job", event_log.NG, code=code, message=message,
                detail=" / ".join(p for p in (outcome.stuck_detail(), stderr[:2000]) if p),
                app=app, mode=mode, finish_code=finish_code, timed_out=timed_out,
                stage=stage_label(stage), windows=windows,
                word_pid=word_pid,
                reused_excel=reused, excel_pid=session.excel.pid if session.excel else None,
                printer=session.printer, elapsed_ms=outcome.elapsed_ms,
                results={k: v for k, v in results.items() if not k.startswith("png")},
                worker_log=logs[-30:])
        else:
            self.log.info("Excel処理終了 mode=%s %d ms", mode, outcome.elapsed_ms)
        return outcome

    @staticmethod
    def _job_failure(results: Dict[str, str], timed_out: bool, finish_code: Optional[int],
                     app: str) -> Tuple[str, str]:
        """記録に残す、処理全体の失敗の種類と文。"""
        kind = "word" if app == "Word" else "excel"
        if results.get("fatal.code"):
            code = results["fatal.code"]
            return code, results.get("fatal.message", "") or message_for(code, kind)
        if timed_out:
            return "TIMEOUT", f"{app} が応答しませんでした"
        if finish_code is None:
            return "WORKER_FAILED", f"{app} を操作する処理プログラムが途中で終わりました"
        items = sorted((k for k in results if k.startswith("item.") and k.endswith(".code") and results[k]),
                       key=lambda k: _int(k.split(".")[1]))
        if items:
            code = results[items[0]]
            return code, message_for(code, kind)
        return "WORKER_FAILED", f"処理プログラムが失敗を返しました(終了コード {finish_code})"

    def _current_printer(self) -> str:
        try:
            return self.printer_provider() or ""
        except Exception:                              # noqa: BLE001 - 取れなければ比べない
            return ""

    def warm_up(self) -> None:
        """Excel を先に起動しておく(点検表を選び始めたとき)。起動済みなら残す時間を延ばすだけ。"""
        with self._session_lock:
            self._cancel_idle_timer()
            if self._session is not None and self._session.worker.alive():
                self._schedule_idle_close()
                return
            self.log.info("Excelを先に起動しておきます(点検表が選ばれたため)")
            self._run_job_locked("selftest", self._base_params(), lambda *a: None, 60)

    # ---- 使い回しの後始末 ------------------------------------------
    def _schedule_idle_close(self) -> None:
        timer = threading.Timer(self.keep_alive_sec, self._idle_close)
        timer.daemon = True
        timer.name = "excel-idle-close"
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_close(self) -> None:
        # 処理が始まっていれば閉じない(終わった処理がまた予約する)
        if not self._session_lock.acquire(blocking=False):
            return
        try:
            if self._session is not None:
                self._close_session(self._session, force=False,
                                    reason=f"{self.keep_alive_sec}秒 使われなかった")
        finally:
            self._session_lock.release()

    def shutdown(self, reason: str = "アプリの終了") -> None:
        """使い回している Excel を閉じる(アプリの終了・診断のあと)。"""
        if not self._session_lock.acquire(timeout=30):
            self.log.warning("Excel処理が終わらないため、閉じずに終了します")
            return
        try:
            self._cancel_idle_timer()
            if self._session is not None:
                self._close_session(self._session, force=False, reason=reason)
        finally:
            self._session_lock.release()

    def _close_session(self, session: "_Session", *, force: bool, reason: str) -> None:
        """標準入力を閉じる → VBScript が Excel を終了して抜ける。残れば止める。"""
        worker = session.worker
        worker.close_stdin()
        if force:
            worker.kill()
        if worker.wait(5 if force else self.excel_cfg.excel_exit_grace_sec + 5) is None:
            worker.kill()
            worker.wait(5)
        if session.cscript:
            self.registry.remove(session.cscript.pid)
        self._ensure_excel_exit(session.excel, force=force)
        if session.word is not None:
            self._ensure_exit(session.word, force=force, exe="winword.exe", label="Word")
            session.word = None
        if self._session is session:
            self._session = None
        self.log.info("Excelを閉じました(%s・%d件処理)", reason, session.jobs)
        event_log.record("excel.close", event_log.INFO, reason=reason, jobs=session.jobs, forced=force,
                         excel_pid=session.excel.pid if session.excel else None, printer=session.printer)

    def _track_excel(self, hwnd_text: str) -> Optional[ProcessIdentity]:
        return self._track_app(hwnd_text, "excel")

    def _track_app(self, hwnd_text: str, app: str) -> Optional[ProcessIdentity]:
        label = "Word" if app == "word" else "Excel"
        hwnd = _int(hwnd_text)
        pid = pid_from_hwnd(hwnd) if hwnd else None
        identity = get_process_identity(pid) if pid else None
        if identity:
            self.registry.add(identity, app)
            self.log.info("%sを起動しました pid=%s", label, identity.pid)
            event_log.record(f"{app}.open", event_log.INFO, **{f"{app}_pid": identity.pid},
                             printer=self._current_printer())
        else:
            self.log.warning("%s の PID を取得できませんでした hwnd=%s", label, hwnd_text)
        return identity

    def _identify_word(self, pid: int) -> Optional[ProcessIdentity]:
        """`WORDPID` の PID。**確かに WINWORD.EXE のときだけ**使う(止めることがあるため)。"""
        identity = get_process_identity(pid) if pid > 0 else None
        if identity is None:
            self.log.warning("Word の PID を確かめられませんでした pid=%s", pid)
            return None
        if IS_WINDOWS and os.path.basename(identity.exe or "").lower() != "winword.exe":
            self.log.warning("Word の PID が WINWORD.EXE ではないため使いません pid=%s exe=%s", pid, identity.exe)
            return None
        self.registry.add(identity, "word")
        self.log.info("Wordを起動しました pid=%s", identity.pid)
        event_log.record("word.open", event_log.INFO, word_pid=identity.pid, printer=self._current_printer())
        return identity

    def _adopt_word(self, session: "_Session", word: Optional[ProcessIdentity]) -> None:
        if word is None:
            return
        if session.word and session.word.pid != word.pid:
            self.registry.remove(session.word.pid)      # 前の Word は終わっていた
        session.word = word

    def _windows_of(self, session: "_Session") -> List[str]:
        """応答しない Excel / Word が出している窓(題名・種類・中の文)。"""
        found: List[str] = []
        for proc, label in ((session.word, "Word"), (session.excel, "Excel")):
            if proc is None:
                continue
            try:
                found += [f"{label} {text}" for text in window_texts(proc.pid)]
            except Exception as exc:                  # noqa: BLE001 - 手がかり集めの失敗で後始末を止めない
                self.log.warning("%s の窓を読めませんでした: %s", label, exc)
        return found

    def _word_shared(self, session: "_Session") -> None:
        """CreateObject が、利用者がすでに開いていた Word を返した(`WORDSHARED`)。

        その Word は利用者のもの。PID を覚えない(覚えていれば手放す)ので、時間切れの後始末・
        起動時の片付けでも止めない。処理プログラムも隠さず・終えない。
        """
        session.word_shared = True
        word, session.word = session.word, None
        if word is not None:
            self.registry.remove(word.pid)
        self.log.warning("Word: 利用者がすでに開いていた Word につながりました。隠さず・終えず・止めません")
        event_log.record("word.shared", event_log.INFO, printer=self._current_printer())

    def _word_lost(self, session: "_Session", where: str) -> None:
        """Word が途中で終わった(`WORDLOST`)。処理プログラムが起こし直して1回だけやり直す。

        残っていれば片付けるのは、こちらが起こした Word(WORDPID・窓で覚えたもの)だけ。
        利用者の Word(WORDSHARED)は覚えていないので触らない。
        """
        word, session.word = session.word, None
        shared, session.word_shared = session.word_shared, False
        self.log.warning("Word が途中で終わりました(%s) pid=%s", stage_label(where) or where or "(不明)",
                         word.pid if word else "-")
        event_log.record("word.lost", event_log.INFO, stage=stage_label(where) or where,
                         word_pid=word.pid if word else None, shared=shared)
        if word is not None:
            self._ensure_exit(word, force=False, exe="winword.exe", label="Word")

    def _word_closed(self, session: "_Session", *, quit_: bool) -> None:
        """処理の終わりに Word を終えた(`WORDBYE 1`)/ 利用者の文書があるので残した(`0`)。"""
        word, session.word = session.word, None
        if word is None:
            return
        if quit_:
            self._ensure_exit(word, force=False, exe="winword.exe", label="Word")
        else:
            # 利用者の文書が開いている Word は、こちらのものではなくなった。止めない
            self.registry.remove(word.pid)
            self.log.info("Word に利用者の文書が開いているため、終えずに残しました pid=%s", word.pid)

    def _ensure_excel_exit(self, excel: Optional[ProcessIdentity], force: bool) -> None:
        """Excel が確実に終了したことを確認する（残っていればその PID だけ強制終了）。"""
        self._ensure_exit(excel, force=force, exe="excel.exe", label="Excel")

    def _ensure_exit(self, proc: Optional[ProcessIdentity], *, force: bool, exe: str, label: str) -> None:
        if proc is None:
            return
        grace = 0 if force else self.excel_cfg.excel_exit_grace_sec
        if not wait_for_exit(proc.pid, proc.create_time, grace):
            self.log.warning("%sプロセスが終了しないため強制終了します pid=%s", label, proc.pid)
            if not terminate_verified(proc.pid, proc.create_time, [exe]):
                self.log.error("%sプロセスを終了できませんでした pid=%s", label, proc.pid)
                return
        self.registry.remove(proc.pid)

    @staticmethod
    def _detail(results: Dict[str, str], index: int) -> str:
        """Excel / Word の返答(文・エラー番号とその意味)と、失敗した所。"""
        errno = results.get(f"item.{index}.errno", "")
        step = results.get(f"item.{index}.step", "")
        parts = [results.get(f"item.{index}.message", ""), explain_errno(errno) if errno else "",
                 f"(失敗した所: {stage_label(step)})" if step else ""]
        return " ".join(p for p in parts if p).strip()


# ======================================================================
# 模擬実装（Excel なしで画面や流れを確認する用途）
# ======================================================================
class DummyExcelBackend:
    name = "dummy"

    def __init__(self, print_delay: float = 1.2, preview_delay: float = 0.6):
        self.print_delay = print_delay
        self.preview_delay = preview_delay

    def preview(self, path: str) -> PreviewImage:
        time.sleep(self.preview_delay)
        if not os.path.isfile(path):
            raise ExcelError("FILE_NOT_FOUND", path)
        name = os.path.splitext(os.path.basename(path))[0]
        return PreviewImage(_dummy_svg(name).encode("utf-8"), "image/svg+xml", sheet="Sheet1",
                            range_address="A1:P40", range_source="dummy", method="dummy", width=794, height=1123)

    def print_files(self, files: List[str], copies: int, on_event: Callable[..., None],
                    should_continue: Callable[[], bool]) -> List[ItemOutcome]:
        outcomes: List[ItemOutcome] = []
        for index, path in enumerate(files, 1):
            item = ItemOutcome(index)
            outcomes.append(item)
            if not should_continue():
                item.code, item.message = "STOPPED", message_for("STOPPED")
                continue
            on_event("begin", index)
            time.sleep(self.print_delay)
            base = os.path.basename(path).lower()
            item.processed = True
            if not os.path.isfile(path):
                item.code = "FILE_NOT_FOUND"
            elif "error" in base or "エラー" in base:
                item.code, item.detail = "PRINT_FAILED", "模擬エラー（ファイル名に「エラー」を含むため）"
            else:
                item.ok, item.sheet = True, "Sheet1"
            item.message = "" if item.ok else message_for(item.code)
            on_event("end", index, item.ok)
        return outcomes

    def selftest(self) -> Dict[str, Any]:
        return {"ok": True, "version": "dummy", "code": "", "detail": "模擬モード", "seconds": 0}


def _dummy_svg(title: str) -> str:
    from html import escape
    rows = "".join(
        f'<rect x="60" y="{150 + i * 38}" width="674" height="38" fill="{"#f4f6f8" if i % 2 else "#fff"}" '
        f'stroke="#c9ced6"/><text x="72" y="{175 + i * 38}" font-size="15" fill="#555">点検項目 {i + 1}</text>'
        f'<rect x="560" y="{158 + i * 38}" width="22" height="22" fill="none" stroke="#888"/>'
        f'<rect x="620" y="{158 + i * 38}" width="22" height="22" fill="none" stroke="#888"/>'
        for i in range(22))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="794" height="1123" viewBox="0 0 794 1123">'
        '<rect width="794" height="1123" fill="#fff"/>'
        f'<text x="397" y="80" text-anchor="middle" font-size="30" font-weight="bold" fill="#222">{escape(title)}</text>'
        '<text x="397" y="115" text-anchor="middle" font-size="15" fill="#c0392b">'
        '模擬プレビュー（Excelを使用していません）</text>'
        f'{rows}'
        '<text x="60" y="1060" font-size="14" fill="#555">点検者：＿＿＿＿＿＿＿＿　　確認者：＿＿＿＿＿＿＿＿</text>'
        '</svg>')


# ======================================================================
# サービス
# ======================================================================
class ExcelService:
    """Excel 処理の窓口。Excel は同時に1つの処理だけ実行する。"""

    # 順番待ちのあいだ「もう要らなくなったか」を見る間隔(秒)
    GIVE_UP_POLL_SEC = 0.2

    def __init__(self, cfg: Any, logger: Any, registry: Optional[TrackedProcessRegistry] = None,
                 work_dir: Optional[str] = None):
        from core import app_config, process_tracking
        self.cfg = cfg
        self.log = logger
        self.registry = registry or process_tracking.registry(logger=logger)
        work_dir = work_dir or str(app_config.local_dir("work"))
        self._lock = threading.Lock()
        self._activity: Optional[str] = None
        backend = cfg.excel.backend
        if backend == "auto":
            backend = "vbscript" if IS_WINDOWS else "dummy"
        if backend == "dummy":
            self.backend: Any = DummyExcelBackend()
        else:
            self.backend = VbsExcelBackend(cfg, work_dir, self.registry, logger)
        self.log.info("Excel連携方式: %s", self.backend.name)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def activity(self) -> Optional[str]:
        return self._activity

    @contextmanager
    def acquire(self, activity: str, timeout: float,
                give_up: Optional[Callable[[], bool]] = None) -> Iterator[None]:
        """Excel の順番を取る。空くまで最大 `timeout` 秒待つ。

        `give_up` を渡すと、待っているあいだそれを見て、真になったら待つのを
        やめる(`ExcelError("SUPERSEDED")`)。プレビューで次の点検表に移ったのに、
        前の要求が順番を待ち続けて Excel とサーバの手を塞がないようにする。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if give_up is not None and give_up():
                raise ExcelError("SUPERSEDED")
            remaining = deadline - time.monotonic()
            slice_ = remaining if give_up is None else min(remaining, self.GIVE_UP_POLL_SEC)
            if self._lock.acquire(timeout=max(0.0, slice_)):
                break
            if remaining <= 0 or give_up is None:
                current = {"print": "印刷", "preview": "プレビュー"}.get(self._activity or "", "他の処理")
                raise ExcelError("EXCEL_BUSY", message=f"{current}の処理中です。完了後に再度お試しください。")
        self._activity = activity
        try:
            yield
        finally:
            self._activity = None
            self._lock.release()

    def preview(self, path: str) -> PreviewImage:
        if not os.path.isfile(path):
            raise ExcelError("FILE_NOT_FOUND", path)
        return self.backend.preview(path)

    def print_files(self, files: List[str], copies: int, on_event: Callable[..., None],
                    should_continue: Callable[[], bool]) -> List[ItemOutcome]:
        return self.backend.print_files(files, copies, on_event, should_continue)

    def selftest(self, keep: bool = False) -> Dict[str, Any]:
        """Excel を起動できるか確かめる。

        `keep=True` のときは閉じずに残す(アプリの中で確かめたとき)。
        閉じきるまで PC によって十数秒かかるうえ、すぐ次のプレビューで
        また起動することになる。残した Excel は使われなければ
        `keep_alive_sec` のあとで閉じる。
        """
        try:
            with self.acquire("prepare" if keep else "selftest", 5):
                try:
                    return self.backend.selftest()
                finally:
                    if not keep:
                        self.shutdown("動作確認のあと")
        except ExcelError as exc:
            return {"ok": False, "code": exc.code, "detail": exc.detail or exc.message, "version": ""}

    def warm_up(self) -> bool:
        """裏で Excel を起動しておく。**ほかの処理中なら何もしない**(待たせない)。

        プレビュー・印刷がそのあいだに押されたら、起動が終わるのを待ってから
        同じ Excel で始まる(1から起動するより早い)。
        """
        warm = getattr(self.backend, "warm_up", None)
        if warm is None or self._lock.locked():
            return False

        def run() -> None:
            try:
                with self.acquire("prepare", timeout=0):
                    warm()
            except ExcelError:
                pass                                  # ほかの処理が先に始まった
            except Exception:                         # noqa: BLE001 - 先回りの失敗で画面を止めない
                self.log.exception("Excelの先回り起動に失敗しました")

        threading.Thread(target=run, name="excel-warm-up", daemon=True).start()
        return True

    def shutdown(self, reason: str = "アプリの終了") -> None:
        """使い回している Excel を閉じる。"""
        close = getattr(self.backend, "shutdown", None)
        if close is not None:
            close(reason)

    def cleanup_leftovers(self, reason: str) -> int:
        """前回の異常終了などで残った Excel / cscript を片付ける（記録した PID のみ）。"""
        cleaned = self.registry.cleanup(reason, ["excel.exe", "winword.exe", "cscript.exe"] if IS_WINDOWS else None)
        return len(cleaned)


def _file_digest(path: str) -> str:
    try:
        with open(path, "rb") as fp:
            return hashlib.sha1(fp.read()).hexdigest()
    except OSError:
        return ""


def _int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def _close_quietly(stream: Any) -> None:
    try:
        if stream is not None:
            stream.close()
    except (OSError, ValueError):
        pass

