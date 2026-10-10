#!/usr/bin/env python3
r"""配布用フォルダを作る (python-web-tools の `scripts/make_dist.py` と同じ)

【なぜ要るのか】
配るときに手でフォルダをコピーすると、**配ってはいけないもの**が紛れます。

    tests\ / __pycache__ / .git     … 開発にしか使わないもの
    config\app.local.json          … その端末だけの上書き
    配布先\ / config\site.json     … 前の版の配布用の設定(`配布設定\` に替わった)

このツールの端末ごとの中身(手元のDB・この端末の設定)は
`%LOCALAPPDATA%\NippouTool` にあり、ツールのフォルダには入っていません。
それでも手で写すと上のものが紛れるので、**配るものだけ**を新しいフォルダへ
写します。設定を一緒に配りたいときは、先に設定画面の「この端末と配布」→
「配布設定を書き出す」で書き出しておいてください(起動用の Start.vbs と
同じフォルダの `配布設定\`。配った先が起動時に読み込みます)。

    python scripts\make_dist.py                     # ツールの隣に「日報管理ツール_VERx.y.z」
    python scripts\make_dist.py --out D:\配布\今回   # 置き場所を指定
    python scripts\make_dist.py --zip               # zip も作る
    python scripts\make_dist.py --no-settings       # 配布設定を入れない

【作ったあとに確かめること】
できたフォルダの `配布メモ.txt` に、版・入れた配布設定・配った先ですることを
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
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 直下で**配るもの**。直下に何かを足したら、ここか `DEV_ONLY` のどちらかに
# 入れる(`tests/test_make_dist.py` が食い違いを見つけます)
INCLUDE: tuple[str, ...] = (
    "README.md", "requirements.txt",
    "Start.vbs", "start.bat", "stop.bat",
    "start_app.py", "server.py", "boot_server.py", "launch_guard.py",
    "process_manager.py",
    # デスクトップ版(日報複合ツールの窓)の入口。ポートを使わない(標準入出力)
    "bridge.py",
    "app", "config", "dbkit", "nippou", "scripts",
    # VC長さ計算の仕様の記録(vc-calculator から移した VBA 解析)。README が指す
    "docs",
)

# 直下にあっても**配らないもの**(開発にしか使わない・端末ごと・作ったもの)
DEV_ONLY: tuple[str, ...] = (
    "tests", ".git", ".gitignore", ".gitattributes", "__pycache__",
    "配布設定", "配布設定.作成中", "配布先",
)

# 中にあっても写さないもの(名前で見る。フォルダならその下ごと)
EXCLUDE_NAMES: tuple[str, ...] = (
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".coverage", ".coverage.*",
    "htmlcov", "*.tmp", "*.bak-*", ".DS_Store", "Thumbs.db",
    # config\ の中の、その端末だけのもの・前の版の配布用の設定
    "app.local.json", "site.json",
)

# できたフォルダに**入っていてはいけない**もの(最後に確かめる)
FORBIDDEN: tuple[str, ...] = (
    "tests", ".git", "config/app.local.json", "config/site.json", "配布先",
)


def _version() -> str:
    try:
        return json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def _tool_name() -> str:
    try:
        return json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["display_name"]
    except (OSError, ValueError, KeyError):
        return "日報管理ツール"


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


def settings_source() -> Path:
    """入れる配布設定(`nippou/distribution.py` の置き場所と同じ)。"""
    from nippou import distribution
    return distribution.folder()


def _settings_lines(folder: Path) -> list[str]:
    """配布設定の中身を、メモと画面に出す形で(パスワードの値は出さない)。"""
    from nippou import distribution
    path = distribution.settings_path(folder)
    lines: list[str] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return [f"  (読めませんでした: {exc})"]
        bundle = distribution.Bundle(
            settings=distribution._clean(data.get("settings")),
            sounds=distribution._sound_files(folder))
        lines = [f"  {row['label']}: {row['value']}"
                 for row in distribution.contents(bundle)]
        if data.get("created_at"):
            lines.append(f"  (作成 {data.get('created_at')} / {data.get('created_on', '')})")
    return lines or ["  (中身がありません)"]


def build(out: Path, *, with_settings: bool = True, force: bool = False,
          make_zip: bool = False,
          settings_src: Optional[Path] = None) -> tuple[Path, list[str]]:
    """配布用フォルダを作る。戻り値は (できたフォルダ, 画面に出す行)。

    断るときは `SystemExit`(理由の文つき)。
    """
    out = out.resolve()
    if _inside(out, ROOT):
        raise SystemExit(f"ツールのフォルダの中には作れません: {out}\n"
                         "(次に作るとき、前に作ったものまで写してしまいます)")
    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(f"{out} はもうあって、中身があります。\n"
                             "別の場所を --out で指定するか、--force で作り直してください。")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    missing = []
    for name in INCLUDE:
        src = ROOT / name
        if not src.exists():
            missing.append(name)
            continue
        if src.is_dir():
            shutil.copytree(src, out / name, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, out / name)
    if missing:
        raise SystemExit("配るはずのファイルがありません: " + ", ".join(missing))

    # 配布設定: 入れる/入れないを**はっきり決める**(--no-settings)。
    # 置くのは**起動用の Start.vbs と同じフォルダ**(配った先が起動時に読む)
    from nippou import distribution
    source = settings_src or settings_source()
    dest = out / distribution.DIR_NAME
    lines = [f"配布用フォルダを作りました: {out}", f"版: VER{_version()}"]
    if with_settings and source.is_dir():
        shutil.copytree(source, dest, ignore=_ignore)
        lines.append(f"{distribution.DIR_NAME} フォルダを入れました"
                     "(起動用の Start.vbs と同じフォルダ。配った先が起動時に読み込みます):")
        lines += _settings_lines(dest)
    else:
        lines.append("配布設定は入れていません。配った先で1台ずつ設定画面から"
                     "参照パスを設定してください。")
        if with_settings:
            lines.append("  (設定画面の「この端末と配布」→「配布設定を書き出す」で"
                         "書き出すと、次からは一緒に配れます)")

    # 入っていてはいけないものが無いか、最後に確かめる
    leaked = [p for p in FORBIDDEN if (out / p).exists()]
    leaked += [str(p.relative_to(out)) for p in out.rglob("*") if _excluded(p.name)]
    if leaked:
        shutil.rmtree(out)
        raise SystemExit("配ってはいけないものが入ったため、作るのをやめました: "
                         + ", ".join(sorted(set(leaked))))

    files = sum(1 for p in out.rglob("*") if p.is_file())
    lines.append(f"ファイル数: {files}")
    (out / "配布メモ.txt").write_text(_memo(lines), encoding="utf-8-sig")

    if make_zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=out.parent,
                                      base_dir=out.name)
        lines.append(f"zip も作りました: {archive}")
    return out, lines


def _memo(lines: list[str]) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        f"{_tool_name()} 配布メモ({today})",
        "",
        *lines,
        "",
        "配った先ですること",
        "  1. このフォルダを好きな場所に置く(以前の版のフォルダに上書きしない)",
        "  2. Python 3.9 以降と、Flask・waitress が入っているか確かめる",
        "     (入っていなければ: python -m pip install -r requirements.txt)",
        "  3. Start.vbs で起動する。配布設定があれば、このとき読み込みます",
        "     (その端末にすでにある設定は上書きしません)",
        "  4. 設定・管理者 →「この端末と配布」→「この端末のライン」でラインを決める",
        "     (ラインは端末ごとに違います。決めるまでは日報入力の画面に知らせが出ます)",
        "  5. 以前の版を使っていた端末は、この端末の設定と手元の日報がそのまま残ります",
        "     (%LOCALAPPDATA%\\NippouTool にあり、ツールのフォルダには入っていないため)",
        "",
        "入れていないもの: tests・__pycache__・前の版の配布用の設定(配布先・config\\site.json)",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配布用フォルダを作る")
    parser.add_argument("--out", help="作る場所(既定: ツールの隣に「日報管理ツール_VER版」)")
    parser.add_argument("--no-settings", action="store_true",
                        help="配布設定(起動用の Start.vbs と同じフォルダの 配布設定)を入れない")
    parser.add_argument("--force", action="store_true",
                        help="作る場所に中身があれば消して作り直す")
    parser.add_argument("--zip", action="store_true", help="zip も作る")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else ROOT.parent / f"{_tool_name()}_VER{_version()}"
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
