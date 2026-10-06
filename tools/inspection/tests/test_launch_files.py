r"""起動ファイル(.bat / .vbs)の作りを固定する(python-web-tools と同じ検査)

**Linux上で編集して、Windowsで実行する**ので、文字コードと改行は
機械的に押さえておかないと静かに壊れる。壊れたことは現場で
「ダブルクリックしても何も起きない」として現れる。

- `.bat` は cmd.exe がコンソールのコードページ(932)で読む
- `.vbs` は WSH がシステムANSI(932)で読む
- CP932 の2バイト目には `\`(0x5C)や `@`(0x40)が現れる。コードページが
  ずれると解釈まで変わるので、`chcp 932` を非ASCIIより前に置く
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

BATCH_FILES = ("start.bat", "stop.bat")
VBS_FILES = ("Start.vbs",)
LAUNCH_FILES = BATCH_FILES + VBS_FILES
MARKER = "点検表 選択・印刷"
CHCP = "chcp 932"


def read_bytes(name: str) -> bytes:
    return (_ROOT / name).read_bytes()


def read_text(name: str) -> str:
    return read_bytes(name).decode("cp932")


def first_non_ascii(data: bytes) -> int:
    for i, byte in enumerate(data):
        if byte > 0x7F:
            return i
    return len(data)


def _commands(text: str) -> list:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":") or stripped.lower().startswith("rem"):
            continue
        lines.append(stripped)
    return lines


class EncodingTests(unittest.TestCase):
    def test_起動ファイルはこれだけ(self) -> None:
        """入口を増やさない(基盤仕様書 2.1)。"""
        found = {p.name for p in _ROOT.glob("*.bat")} | {p.name for p in _ROOT.glob("*.vbs")}
        self.assertEqual(found, set(LAUNCH_FILES))

    def test_CP932として意味が通る(self) -> None:
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                self.assertIn(MARKER, read_text(name), f"{name} がUTF-8等で保存されていませんか")

    def test_BOMを付けない(self) -> None:
        for name in LAUNCH_FILES:
            with self.subTest(name=name):
                data = read_bytes(name)
                self.assertNotEqual(data[:3], b"\xef\xbb\xbf")
                self.assertNotIn(data[:2], (b"\xff\xfe", b"\xfe\xff"))

    def test_改行はCRLF(self) -> None:
        for name in LAUNCH_FILES + ("app/vbscript/excel_worker.vbs",):
            with self.subTest(name=name):
                data = read_bytes(name)
                self.assertEqual(data.count(b"\n") - data.count(b"\r\n"), 0, f"{name} に単独のLFがあります")
                self.assertEqual(data.count(b"\r") - data.count(b"\r\n"), 0, f"{name} に単独のCRがあります")

    def test_gitが改行と文字コードを変えない(self) -> None:
        """フォルダごとコピーして配る経路では git の変換が効かないので、
        バイト列のまま保存させる(`-text`)。"""
        text = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
        for pattern in ("*.bat", "*.vbs"):
            with self.subTest(pattern=pattern):
                self.assertRegex(text, rf"(?m)^{re.escape(pattern)}\s+-text\s*$")


class BatchTests(unittest.TestCase):
    def test_日本語より前にコードページを決める(self) -> None:
        for name in BATCH_FILES:
            with self.subTest(name=name):
                data = read_bytes(name)
                position = data.find(CHCP.encode("ascii"))
                self.assertNotEqual(position, -1)
                self.assertLess(position, first_non_ascii(data))

    def test_共有フォルダでも移動できる(self) -> None:
        """`cd /d` は UNCパスを現在地にできない。`pushd` を使う。"""
        for name in BATCH_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                self.assertIn('pushd "%~dp0"', text)
                for line in _commands(text):
                    self.assertNotIn("cd /d", line)

    def test_pushdとpopdの数が合う(self) -> None:
        for name in BATCH_FILES:
            with self.subTest(name=name):
                text = read_text(name)
                pushd = len(re.findall(r"^\s*pushd\b", text, re.MULTILINE))
                popd = len(re.findall(r"^\s*popd\b", text, re.MULTILINE))
                self.assertGreaterEqual(popd, pushd)

    def test_括弧の中に半角括弧を書かない(self) -> None:
        """`if ( ... )` の中の `)` はブロックの終わりとして読まれる。"""
        for name in BATCH_FILES:
            with self.subTest(name=name):
                depth = 0
                for lineno, line in enumerate(read_text(name).splitlines(), 1):
                    stripped = line.strip()
                    if stripped.lower().startswith("rem"):
                        continue
                    if depth > 0 and stripped.lower().startswith("echo "):
                        body = stripped[5:]
                        self.assertNotIn("(", body, f"{name}:{lineno}")
                        self.assertNotIn(")", body, f"{name}:{lineno}")
                        continue
                    depth = max(0, depth + line.count("(") - line.count(")"))

    def test_失敗したら理由を読ませてから閉じる(self) -> None:
        for name in BATCH_FILES:
            with self.subTest(name=name):
                self.assertIn("pause", read_text(name))

    def test_呼び出す先が実在する(self) -> None:
        for name, target in (("start.bat", "start_app.py"), ("stop.bat", "process_manager.py")):
            with self.subTest(name=name):
                self.assertIn(target, read_text(name))
                self.assertTrue((_ROOT / target).exists())

    def test_引数はそのまま渡す(self) -> None:
        self.assertIn("python start_app.py --check-excel %*", read_text("start.bat"))
        self.assertIn("python process_manager.py %*", read_text("stop.bat"))

    def test_診断起動はExcelもブラウザを開いたあとに確かめる(self) -> None:
        # 先に確かめると、Excel を起動して閉じきるまで画面が出なかった(実機で十数秒)
        text = read_text("start.bat")
        self.assertIn("python start_app.py --check\r\n", text)
        self.assertNotIn("--check --check-excel", text)
        self.assertLess(text.index("python start_app.py --check\r\n"),
                        text.index("python start_app.py --check-excel %*"))


class VbsTests(unittest.TestCase):
    def test_Option_Explicitがある(self) -> None:
        self.assertIn("Option Explicit", read_text("Start.vbs"))

    def test_宣言した変数を全部使う(self) -> None:
        text = read_text("Start.vbs")
        declared = re.search(r"^Dim (.+)$", text, re.MULTILINE)
        self.assertIsNotNone(declared)
        for var in (v.strip() for v in declared.group(1).split(",")):
            self.assertGreater(len(re.findall(rf"\b{re.escape(var)}\b", text)), 1, var)

    def test_本体とpythonwの有無を確かめる(self) -> None:
        text = read_text("Start.vbs")
        self.assertIn("FileExists", text)
        self.assertIn("cmd /c python --version", text)
        self.assertIn("cmd /c pythonw --version", text)

    def test_絶対パスで渡して引用する(self) -> None:
        text = read_text("Start.vbs")
        self.assertIn("BuildPath", text)
        self.assertIn("Chr(34)", text)

    def test_知らせたら止まる(self) -> None:
        text = read_text("Start.vbs")
        self.assertEqual(text.count("WScript.Quit 1"), text.count("MsgBox "))


if __name__ == "__main__":
    unittest.main()
