#!/usr/bin/env python3
r"""統合ツール一式の配布用フォルダを作る

【なぜ要るのか】
配るときに手でフォルダをコピーすると、**配ってはいけないもの**が紛れます
(tests\・__pycache__・.git・src-tauri\target\(数 GB)・端末ごとの設定)。
このスクリプトは**配るものだけ**を新しいフォルダへ写します。

    python scripts\make_dist.py                     # 一式の隣に「統合ツール_VERx.y.z」
    python scripts\make_dist.py --out D:\配布\今回   # 置き場所を指定
    python scripts\make_dist.py --zip               # zip も作る
    python scripts\make_dist.py --no-settings       # 各ツールの配布設定を入れない
    python scripts\make_dist.py --exe D:\AllTools.exe  # 入れる exe を指定

【中身】

    統合ツール_VERx.y.z\
      統合ツール.exe          デスクトップ版(GitHub Actions の成果物 AllTools-windows の
                              AllTools.exe。一式の直下に置いてあれば、この名前で入れる)
      Start.vbs / start.bat / stop.bat   ブラウザ版(予備)
      bridge.py start_app.py process_manager.py requirements.txt README.md
      config\ portal\ docs\ scripts\
      tools\<ツール>\         各ツール。**そのツールの make_dist が配るもの**を、そのツールの
                              決まり(配ってはいけないもの・配布設定)のまま写す
      配布メモ.txt            版・入れたもの・配った先ですること

各ツールの配布設定(各ツールの設定画面で書き出す `tools\<ツール>\配布設定\`)は、
そのツールの決まりどおりに入れます(`--no-settings` で入れない)。
大設定(タブ表示権限)は共有の DB にあるので、配る必要はありません。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

#: 一式の直下で**配るもの**。直下に何かを足したら、ここか ``DEV_ONLY`` のどちらかに
#: 入れる(``tests/test_make_dist.py`` が食い違いを見つけます)
INCLUDE: tuple[str, ...] = (
    "README.md", "requirements.txt",
    "Start.vbs", "start.bat", "stop.bat",
    "start_app.py", "process_manager.py",
    # デスクトップ版(統合ツール.exe)の入口。ポートを使わない(標準入出力)
    "bridge.py",
    "config", "portal", "docs", "scripts",
)

#: 直下にあっても**配らないもの**(開発にしか使わない・作ったもの)。
#: ``tools`` はツールごとに、そのツールの決まりで写す(下の ``TOOL_BUILDERS``)
DEV_ONLY: tuple[str, ...] = (
    "tests", "src-tauri", ".github", ".git", ".gitignore", ".gitattributes",
    "__pycache__", "logs", "data", "tools",
)

#: 中にあっても写さないもの(名前で見る。フォルダならその下ごと)
EXCLUDE_NAMES: tuple[str, ...] = (
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".coverage", ".coverage.*",
    "htmlcov", "*.tmp", "*.bak-*", ".DS_Store", "Thumbs.db",
    "*.sqlite3", "*.sqlite3-*", "*.accdb", "*.laccdb",
)

#: できたフォルダに**入っていてはいけない**もの(最後に確かめる)
FORBIDDEN: tuple[str, ...] = ("tests", ".git", "src-tauri", "data", "logs")

#: デスクトップ版の exe。配るときの名前と、探す場所(先に見つかったもの)
EXE_NAME = "統合ツール.exe"
EXE_CANDIDATES: tuple[str, ...] = (
    "統合ツール.exe",
    "AllTools.exe",
    "src-tauri/target/release/AllTools.exe",
)

#: ツールの make_dist(そのツールが配るもの・配ってはいけないもの・配布設定の決まりを持つ)。
#: 無いツールは ``GENERIC_INCLUDE`` の一覧で写す
TOOL_BUILDERS: dict[str, str] = {
    "nippou": "scripts/make_dist.py",
    "kanban": "scripts/make_dist.py",
    "calendar": "tools/make_dist.py",
}

#: make_dist を持たないツールの、配るもの(そのツールの直下)
GENERIC_INCLUDE: dict[str, tuple[str, ...]] = {
    "inspection": (
        "README.md", "requirements.txt", "Start.vbs", "start.bat", "stop.bat",
        "start_app.py", "server.py", "boot_server.py", "launch_guard.py", "process_manager.py",
        "bridge.py", "loading.html",
        "app", "assets", "config", "core", "data", "docs",
    ),
}

#: 各ツールの配布設定のフォルダ名(どのツールも同じ名前)
SETTINGS = "配布設定"


def _catalog(root: Path = ROOT) -> dict:
    return json.loads((root / "config" / "tools.json").read_text(encoding="utf-8-sig"))


def _app(root: Path = ROOT) -> dict:
    return json.loads((root / "config" / "app.json").read_text(encoding="utf-8-sig"))


def version(root: Path = ROOT) -> str:
    return str(_app(root).get("version", "0.0.0"))


def display_name(root: Path = ROOT) -> str:
    return str(_app(root).get("display_name", "統合ツール"))


def _tool_version(tool_dir: Path) -> str:
    try:
        return str(json.loads((tool_dir / "config" / "app.json").read_text(encoding="utf-8-sig"))
                   .get("version", ""))
    except (OSError, ValueError):
        return ""


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_NAMES)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _excluded(name)}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def find_exe(root: Path = ROOT) -> Optional[Path]:
    for name in EXE_CANDIDATES:
        path = root / name
        if path.is_file():
            return path
    return None


def _load_builder(tool_id: str, tool_dir: Path):
    """そのツールの make_dist を読み込む(scripts はパッケージではないので、場所で読む)。"""
    path = tool_dir / TOOL_BUILDERS[tool_id]
    spec = importlib.util.spec_from_file_location(f"alltools_make_dist_{tool_id}", path)
    module = importlib.util.module_from_spec(spec)
    # ツールの make_dist は自分のフォルダを sys.path に足して、自分のパッケージを読む。
    # ツールどうしで同じ名前のモジュール(launch_guard・server…)があるので、読み終えたら外す
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
    return module, saved_modules


def _forget_modules(before: set[str]) -> None:
    """ツールの make_dist が読んだモジュールを忘れる(次のツールが同じ名前を読めるように)。"""
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def _build_tool(tool: dict, out: Path, *, with_settings: bool, root: Path) -> list[str]:
    """1つのツールを ``out``(``tools\\<ツール>``)へ写す。戻り値は配布メモに書く行。"""
    tool_id = tool["id"]
    tool_dir = root / tool["dir"]
    title = tool.get("title", tool_id)
    if tool_id in TOOL_BUILDERS:
        saved_path = list(sys.path)
        sys.path.insert(0, str(tool_dir))
        module, before = _load_builder(tool_id, tool_dir)
        try:
            sys.path.insert(0, str(tool_dir))
            _out, lines = module.build(out, with_settings=with_settings)
        finally:
            sys.path[:] = saved_path
            _forget_modules(before)
        # ツールの配布メモは、ツールだけを配るときのもの。統合ツールでは一式の配布メモにまとめる
        (out / "配布メモ.txt").unlink(missing_ok=True)
        keep = [line for line in lines
                if not line.startswith(("配布用フォルダを作りました", "版:", "ファイル数", "zip も作りました"))
                and "統合ツールごと配るときは" not in line]
        return [f"[{title}] VER{_tool_version(tool_dir)}"] + [f"  {line}" for line in keep]

    # make_dist を持たないツール: 一覧のとおりに写す
    missing = []
    out.mkdir(parents=True, exist_ok=True)
    for name in GENERIC_INCLUDE[tool_id]:
        src = tool_dir / name
        if not src.exists():
            missing.append(name)
            continue
        if src.is_dir():
            shutil.copytree(src, out / name, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, out / name)
    if missing:
        raise SystemExit(f"{title}: 配るはずのファイルがありません: " + ", ".join(missing))
    lines = [f"[{title}] VER{_tool_version(tool_dir)}"]
    settings = tool_dir / SETTINGS
    if with_settings and settings.is_dir():
        shutil.copytree(settings, out / SETTINGS, ignore=_ignore)
        lines.append(f"  {SETTINGS} フォルダを入れました(配った先が起動時に読み込みます)")
    else:
        lines.append("  配布設定は入れていません")
    return lines


def build(out: Path, *, with_settings: bool = True, force: bool = False,
          make_zip: bool = False, exe: Optional[Path] = None,
          root: Path = ROOT) -> tuple[Path, list[str]]:
    """配布用フォルダを作る。戻り値は (できたフォルダ, 画面に出す行)。

    断るときは ``SystemExit``(理由の文つき)。
    """
    out = out.resolve()
    if _inside(out, root):
        raise SystemExit(f"統合ツールのフォルダの中には作れません: {out}\n"
                         "(次に作るとき、前に作ったものまで写してしまいます)")
    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(f"{out} はもうあって、中身があります。\n"
                             "別の場所を --out で指定するか、--force で作り直してください。")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    missing = []
    for name in INCLUDE:
        src = root / name
        if not src.exists():
            missing.append(name)
            continue
        if src.is_dir():
            shutil.copytree(src, out / name, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, out / name)
    if missing:
        shutil.rmtree(out, ignore_errors=True)
        raise SystemExit("配るはずのファイルがありません: " + ", ".join(missing))

    lines = [f"配布用フォルダを作りました: {out}", f"版: {display_name(root)} VER{version(root)}"]

    # デスクトップ版の exe(あれば)。一式の直下に置く決まり(bridge.py・config と同じ所)
    exe = exe if exe is not None else find_exe(root)
    if exe is not None and exe.is_file():
        shutil.copy2(exe, out / EXE_NAME)
        lines.append(f"デスクトップ版を入れました: {EXE_NAME}(元: {exe.name})")
    else:
        lines.append(f"デスクトップ版({EXE_NAME})は入っていません。GitHub Actions の"
                     "「デスクトップ版(Windows)」で作った AllTools.exe を一式の直下に置いてから"
                     "作り直してください(無くてもブラウザ版の Start.vbs で動きます)。")

    # 各ツール(そのツールの決まりで写す)
    try:
        for tool in _catalog(root)["tools"]:
            lines += _build_tool(tool, out / tool["dir"], with_settings=with_settings, root=root)
    except SystemExit:
        shutil.rmtree(out, ignore_errors=True)
        raise

    # 入っていてはいけないものが無いか、最後に確かめる
    leaked = [p for p in FORBIDDEN if (out / p).exists()]
    leaked += [str(p.relative_to(out)) for p in out.rglob("*") if _excluded(p.name)]
    leaked += [str(p.relative_to(out)) for p in out.rglob("tests") if p.is_dir()]
    leaked += [str(p.relative_to(out)) for p in out.rglob("src-tauri")]
    if leaked:
        shutil.rmtree(out)
        raise SystemExit("配ってはいけないものが入ったため、作るのをやめました: "
                         + ", ".join(sorted(set(leaked))))

    files = sum(1 for p in out.rglob("*") if p.is_file())
    lines.append(f"ファイル数: {files}")
    (out / "配布メモ.txt").write_text(_memo(lines, root).replace("\n", "\r\n"),
                                      encoding="utf-8-sig", newline="")

    if make_zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=out.parent, base_dir=out.name)
        lines.append(f"zip も作りました: {archive}")
    return out, lines


def _memo(lines: list[str], root: Path) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        f"{display_name(root)} 配布メモ({today})",
        "",
        *lines,
        "",
        "配った先ですること",
        "  1. このフォルダを好きな場所に置く(以前の版のフォルダに上書きするなら、",
        "     各ツールの 配布設定 フォルダを残すかどうかを先に決める)",
        "  2. Python 3.9 以降と、Flask・waitress が入っているか確かめる",
        "     (入っていなければ: python -m pip install -r requirements.txt)",
        f"  3. {EXE_NAME} で起動する(デスクトップ版。ポートを使いません)。",
        "     exe が無いとき・動かないときは Start.vbs(ブラウザ版)で起動できます。",
        "     ブラウザ版とデスクトップ版は同時には動きません(後から開いたほうが止まります)",
        "  4. 大設定のタブで、この端末の ログインID・PC名 と出ているタブを確かめる",
        "     (タブ表示権限は共有の DB の表。登録は大設定から管理者パスワードで行います)",
        "  5. 各ツールのタブの設定画面で、この端末のライン・モードを選ぶ(端末ごとに違います)",
        "",
        "入れていないもの: tests・src-tauri(exe のソース)・端末ごとの設定や手元の DB",
        "",
    ])


def default_out(root: Path = ROOT) -> Path:
    return root.parent / f"{display_name(root)}_VER{version(root)}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="統合ツール一式の配布用フォルダを作る")
    parser.add_argument("--out", help="作る場所(既定: 一式の隣に「統合ツール_VER版」)")
    parser.add_argument("--no-settings", action="store_true",
                        help="各ツールの配布設定(tools\\<ツール>\\配布設定)を入れない")
    parser.add_argument("--force", action="store_true",
                        help="作る場所に中身があれば消して作り直す")
    parser.add_argument("--zip", action="store_true", help="zip も作る")
    parser.add_argument("--exe", help=f"入れるデスクトップ版の exe(既定: 直下の {EXE_CANDIDATES[1]} など)")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else default_out()
    try:
        _out, lines = build(out, with_settings=not args.no_settings, force=args.force,
                            make_zip=args.zip, exe=Path(args.exe) if args.exe else None)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
