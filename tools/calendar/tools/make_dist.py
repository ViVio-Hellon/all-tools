#!/usr/bin/env python3
r"""配布用フォルダを作る

【なぜ要るのか】
配るときに手でフォルダをコピーすると、**配ってはいけないもの**が紛れます。

    tests\ / .git / __pycache__            … 開発にしか使わない
    sync\ / logs\ / print\ / settings.json … 開発機で CALENDAR_HOME をツールの
                                             フォルダに向けたときにできるもの
                                             (その端末の設定・送信待ち)

このスクリプトは**配るものだけ**を新しいフォルダへ写します。設定を一緒に
配りたいときは、先に設定画面の「配布設定」で書き出しておいてください
(ツールの直下の `配布設定\`。配った先が起動時に読み込みます)。

    python tools\make_dist.py                     # ツールの隣に「ライン管理カレンダー_VERx.y.z\ライン管理カレンダー\」
    python tools\make_dist.py --out D:\配布\今回   # 置き場所を指定
    python tools\make_dist.py --zip               # zip も作る(「ライン管理カレンダー_VERx.y.z\ライン管理カレンダー_VERx.y.z.zip」)
    python tools\make_dist.py --no-settings       # 配布設定を入れない

【置き場所 ── 版は親のフォルダーに入れ、作る物は版なし】
業務ツール統合ランチャーの「道を変えない」決まり(起動ファイルの名前と置き場所を変えない。
フォルダー名に版を付けない)に合わせて、版は親のフォルダー(ライン管理カレンダー_VERx.y.z\)に入れ、
作るフォルダーの名前は ライン管理カレンダー\ だけにします。zip のファイル名
(ライン管理カレンダー_VERx.y.z.zip)には版が入りますが、zip の中の最上位は ライン管理カレンダー\ です。
配った先では、決まった場所の ライン管理カレンダー\ に中身ごと上書きします
(フォルダー名を変えない・版の付いた名前で置かない)。

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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 直下で**配るもの**。リポジトリの直下から、開発にしか使わないもの
# (tests・.gitignore・.gitattributes)を除いたもの
INCLUDE: tuple[str, ...] = (
    "README.md", "requirements.txt",
    "Start.vbs", "start.bat", "stop.bat",
    "start_app.py", "server.py", "boot_server.py", "launch_guard.py",
    "process_manager.py",
    # 管理者以外停止モード(日報複合ツールの入口と同じ中身)
    "remote_stop.py",
    # デスクトップ版(日報複合ツールの窓)の入口(日報複合ツールの外枠が子として起動する)
    "bridge.py",
    # 配布設定を書き出すだけの入口(日報複合ツールの大設定の「まとめて書き出す」が起こす)
    "export_distribution.py",
    "app", "calendar_app", "config", "docs", "tools",
)

# デスクトップ版は**日報複合ツールの窓**で使う(カレンダーだけの exe は無い)。
# 日報複合ツールごと配るときは、一式のフォルダの ``scripts\make_dist.bat`` を使う
INTEGRATED_NOTE = ("デスクトップ版は日報複合ツール(日報複合ツール.exe)の窓の「カレンダー」のタブで使います。"
                   "この配布はカレンダーだけのブラウザ版です。日報複合ツールごと配るときは、"
                   "日報複合ツールのフォルダの scripts\\make_dist.bat を使ってください")

# 中にあっても写さないもの(名前で見る。フォルダならその下ごと)
EXCLUDE_NAMES: tuple[str, ...] = (
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".coverage", ".coverage.*",
    "htmlcov", "*.tmp", "*.bak-*", ".DS_Store", "Thumbs.db",
    "*.db", "*.db-journal", "*.sqlite3-journal",
)

# 配布設定のフォルダ(`calendar_app/distribution.py` の置き場所と同じ)。
# **INCLUDE には入れない** ── 入れるかどうかは --no-settings で決める
SETTINGS = Path("配布設定")

# できたフォルダに**入っていてはいけない**もの(最後に確かめる)
FORBIDDEN: tuple[str, ...] = (
    "tests", ".git", "sync", "logs", "print", "settings.json",
)


def _version() -> str:
    try:
        return json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def _display_name() -> str:
    try:
        return json.loads((ROOT / "config" / "app.json").read_text(
            encoding="utf-8"))["display_name"]
    except (OSError, ValueError, KeyError):
        return "ライン管理カレンダー"


def default_out() -> Path:
    """既定の作る場所: ツールの隣の ``<表示名>_VER<版>\\<表示名>``(作る物のフォルダー名は版なし)。"""
    return ROOT.parent / f"{_display_name()}_VER{_version()}" / _display_name()


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
    from calendar_app import admin_password, distribution
    lines = []
    path = distribution.settings_path(folder)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return [f"  (読めませんでした: {exc})"]
        for key, value in (data.get("settings") or {}).items():
            shown = "(設定済み)" if key == admin_password.KEY else value
            if isinstance(value, bool):
                shown = "する" if value else "しない"
            lines.append(f"  {distribution.ITEM_LABELS.get(key, key)}: {shown}")
        if data.get("created_at"):
            lines.append(f"  (作成 {data.get('created_at')} / {data.get('created_on', '')})")
    return lines or ["  (中身がありません)"]


def build(out: Path, *, with_settings: bool = True, force: bool = False,
          make_zip: bool = False) -> tuple[Path, list[str]]:
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

    # 配布設定: 入れる/入れないを**はっきり決める**(--no-settings)
    settings_src = ROOT / SETTINGS
    lines = [f"配布用フォルダを作りました: {out}", f"版: VER{_version()}", INTEGRATED_NOTE]
    if with_settings and settings_src.is_dir():
        shutil.copytree(settings_src, out / SETTINGS, ignore=_ignore)
        lines.append(f"{SETTINGS} フォルダを入れました(配った先が起動時に読み込みます):")
        lines += _settings_lines(out / SETTINGS)
    else:
        lines.append("配布設定は入れていません。配った先で1台ずつ設定画面から"
                     "参照パスを設定してください。")
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
    (out / "配布メモ.txt").write_text(_memo(lines), encoding="utf-8-sig")

    if make_zip:
        archive = make_zip_file(out, _display_name(), _version())
        lines.append(f"zip も作りました: {archive}(中の最上位は {_display_name()}\\)")
    return out, lines


def make_zip_file(out: Path, name: str, ver: str) -> Path:
    """``out`` の中身を、最上位が版なしの ``name\\`` の zip にする。

    zip のファイル名には版を入れる(``<out の親>\\<name>_VER<ver>.zip``)が、展開してできる
    フォルダーは版なしの ``name``(業務ツール統合ランチャーの「道を変えない」決まり)。
    """
    archive = out.parent / f"{name}_VER{ver}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(out, f"{name}/")
        for path in sorted(out.rglob("*")):
            zf.write(path, f"{name}/{path.relative_to(out).as_posix()}")
    return archive


def _memo(lines: list[str]) -> str:
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return "\n".join([
        f"{_display_name()} 配布メモ({today})",
        "",
        *lines,
        "",
        "配った先ですること",
        f"  1. 決まった場所の {_display_name()}\\ に中身ごと上書きする",
        "     (フォルダー名を変えない・版の付いた名前で置かない。業務ツール統合ランチャーは",
        "     決まった場所のフォルダーを起動するため。上書きする前に、",
        "     以前の 配布設定 フォルダを残すかどうかを先に決める)",
        "  2. Python 3.9 以降と、Flask・waitress が入っているか確かめる",
        "     (入っていなければ: python -m pip install -r requirements.txt)",
        "  3. Start.vbs で起動する(ブラウザ版。起動しないときは start.bat で原因が出ます)。",
        "     配布設定があれば、このとき読み込みます",
        "  4. 設定画面の「1. この端末のライン」でラインを選ぶ",
        "     (ラインは端末ごとに違います。マスタ確認の端末一覧で予約もできます)",
        "",
        "入れていないもの: tests・開発機の設定や送信待ち(sync・logs・print・settings.json)",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配布用フォルダを作る")
    parser.add_argument("--out", help="作る場所(既定: ツールの隣に「ライン管理カレンダー_VER版\\表示名」)")
    parser.add_argument("--no-settings", action="store_true",
                        help="配布設定(ツール直下の 配布設定 フォルダ)を入れない")
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
