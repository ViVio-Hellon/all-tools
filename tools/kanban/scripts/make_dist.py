#!/usr/bin/env python3
r"""配布用フォルダを作る(python-web-tools の ``scripts/make_dist.py`` と同じつくり)

【なぜ要るのか】
配るときに手でフォルダをコピーすると、**配ってはいけないもの**が紛れます。

    data\config.json   … 一時期の版がアプリのフォルダに置いていた、その端末の設定。
                         配ると配った先がそれを引き継ぎ、配布設定を読み込まない
    tests\ / __pycache__ / .git …

このスクリプトは**配るものだけ**を新しいフォルダへ写します。設定を一緒に
配りたいときは、先に設定画面の「配布設定」で書き出しておいてください
(アプリの直下の ``配布設定\``。配った先が起動時に読み込みます)。

    python scripts\make_dist.py                     # アプリの隣に「資材発注看板システム_VERx.y.z」
    python scripts\make_dist.py --out D:\配布\今回   # 置き場所を指定
    python scripts\make_dist.py --zip               # zip も作る
    python scripts\make_dist.py --no-settings       # 配布設定を入れない

設定画面の「配布設定」→「配布用フォルダを作る」からも同じものを作れます。

【作ったあとに確かめること】
できたフォルダの ``配布メモ.txt`` に、版・入れた配布設定・配った先ですることを
書いてあります。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 直下で**配るもの**
INCLUDE: tuple[str, ...] = (
    "README.md", "requirements.txt",
    "Start.vbs", "start.bat", "stop.bat", "run_kanban.bat", "run_kanban_warehouse.bat",
    "start_app.py", "server.py", "boot_server.py", "launch_guard.py", "bridge.py",
    "process_manager.py", "main.py",
    "app", "config", "docs", "kanban", "dbkit", "tools", "scripts",
)

#: 中にあっても写さないもの(名前で見る。フォルダならその下ごと)
EXCLUDE_NAMES: tuple[str, ...] = (
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".coverage", ".coverage.*",
    "htmlcov", "*.tmp", "*.bak-*", ".DS_Store", "Thumbs.db",
    "*.sqlite3", "*.sqlite3-*", "*.accdb", "*.laccdb",
)

#: デスクトップ版は**統合ツールの窓**で使う(看板だけの exe は無い)。
#: 統合ツールごと配るときは、一式のフォルダの ``scripts\make_dist.bat`` を使う
INTEGRATED_NOTE = ("デスクトップ版は統合ツール(統合ツール.exe)の窓の「看板」のタブで使います。"
                   "この配布は看板だけのブラウザ版です。統合ツールごと配るときは、"
                   "統合ツールのフォルダの scripts\\make_dist.bat を使ってください")

#: 配布設定のフォルダ(``kanban/distribution.py`` の置き場所と同じ)。
#: **INCLUDE には入れない** ── 入れるかどうかは --no-settings で決める
SETTINGS = Path("配布設定")

#: できたフォルダに**入っていてはいけない**もの(最後に確かめる)
FORBIDDEN: tuple[str, ...] = ("data", "tests", ".git")


def _version() -> str:
    try:
        return json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def default_out(*, stamp: bool = False) -> Path:
    """既定の作る場所(アプリの隣)。``stamp`` なら日時を付けて、前に作ったものと重ねない。"""
    name = f"資材発注看板システム_VER{_version()}"
    if stamp:
        name += dt.datetime.now().strftime("_%Y%m%d_%H%M%S")
    return ROOT.parent / name


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_NAMES)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if _excluded(n)}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _settings_lines(folder: Path) -> list[str]:
    """配布設定の中身を、メモと画面に出す形で(パスワードの値は出さない)。"""
    from kanban import distribution

    bundle = distribution.read(folder)
    if bundle is None:
        return ["  (読めませんでした)"]
    lines = [f"  {distribution.ITEM_LABELS[k]}: {distribution._show(k, v)}"
             for k, v in bundle.settings.items()]
    if bundle.created_at:
        lines.append(f"  (作成 {bundle.created_at} / {bundle.created_on})")
    return lines or ["  (中身がありません)"]


def build(out: Path, *, with_settings: bool = True, force: bool = False,
          make_zip: bool = False, root: Path | None = None) -> tuple[Path, list[str]]:
    """配布用フォルダを作る。戻り値は (できたフォルダ, 画面に出す行)。

    断るときは ``SystemExit``(理由の文つき)。
    """
    root = root or ROOT
    out = out.resolve()
    if _inside(out, root):
        raise SystemExit(f"アプリのフォルダの中には作れません: {out}\n"
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

    desktop_line = INTEGRATED_NOTE

    # 配布設定: 入れる/入れないを**はっきり決める**(--no-settings)
    from kanban import distribution

    settings_src = distribution.directory() if root == ROOT else root / SETTINGS
    lines = [f"配布用フォルダを作りました: {out}", f"版: VER{_version()}", desktop_line]
    if with_settings and settings_src.is_dir():
        shutil.copytree(settings_src, out / SETTINGS, ignore=_ignore)
        lines.append(f"{SETTINGS} フォルダを入れました(配った先が起動時に読み込みます):")
        lines += _settings_lines(out / SETTINGS)
    else:
        lines.append("配布設定は入れていません。配った先で 1 台ずつ設定画面から"
                     "接続先と管理者パスワードを設定してください。")
        if with_settings:
            lines.append("  (設定画面の「配布設定」で書き出すと、次からは一緒に配れます)")

    # 入っていてはいけないものが無いか、最後に確かめる
    leaked = [p for p in FORBIDDEN if (out / p).exists()]
    leaked += [str(p.relative_to(out)) for p in out.rglob("*") if _excluded(p.name)]
    if leaked:
        shutil.rmtree(out)
        raise SystemExit("配ってはいけないものが入ったため、作るのをやめました: "
                         + ", ".join(sorted(set(leaked))))

    files = sum(1 for p in out.rglob("*") if p.is_file())
    lines.append(f"ファイル数: {files}")
    (out / "配布メモ.txt").write_text(_memo(lines).replace("\n", "\r\n"),
                                      encoding="utf-8-sig", newline="")

    if make_zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=out.parent,
                                      base_dir=out.name)
        lines.append(f"zip も作りました: {archive}")
    return out, lines


def _memo(lines: list[str]) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        f"資材発注看板システム 配布メモ({today})",
        "",
        *lines,
        "",
        "配った先ですること",
        "  1. このフォルダを好きな場所に置く(以前の版のフォルダに上書きしない)",
        "  2. Python 3.9 以降と、Flask・waitress が入っているか確かめる",
        "     (入っていなければ: python -m pip install -r requirements.txt)",
        "  3. Start.vbs で起動する(ブラウザ版)。配布設定があれば、このとき読み込みます",
        "  4. 設定画面の「この端末」で担当ラインとモードを選ぶ(端末ごとに違います)",
        "",
        "入れていないもの: data(端末ごとの設定)・tests",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配布用フォルダを作る")
    parser.add_argument("--out", help="作る場所(既定: アプリの隣に「資材発注看板システム_VER版」)")
    parser.add_argument("--no-settings", action="store_true",
                        help="配布設定(アプリ直下の 配布設定 フォルダ)を入れない")
    parser.add_argument("--force", action="store_true",
                        help="作る場所に中身があれば消して作り直す")
    parser.add_argument("--zip", action="store_true", help="zip も作る")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else default_out()
    try:
        _out, lines = build(out, with_settings=not args.no_settings,
                            force=args.force, make_zip=args.zip)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
