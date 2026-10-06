#!/usr/bin/env python3
"""ログから「なぜなぜ分析の材料」を作るコマンドライン版

設定画面の「9. ログ」と同じものを、**集めたログのフォルダ**に対して使う。
ログフォルダを共有フォルダにしてあれば、その下に PC名 ごとのフォルダが
並ぶので、全端末ぶんをまとめて見られる(``--dir`` の下を全部探す)。

使い方::

    # 最近7日の記録番号の一覧(全端末ぶん)
    python tools/log_report.py --dir \\\\サーバ\\共有\\カレンダーログ

    # 記録番号1つぶんをまとめる(画面に出す)
    python tools/log_report.py E20261001-142233-K3Q --dir \\\\サーバ\\共有\\カレンダーログ

    # ファイルに書く(報告書に添える)
    python tools/log_report.py E20261001-142233-K3Q --out なぜなぜ材料.txt

``--dir`` を省くと、この端末のいまの書き先と既定の場所を見る。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calendar_app import log_report  # noqa: E402
from calendar_app.logging_utils import silence_console  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ログから、なぜなぜ分析の材料をまとめます。")
    parser.add_argument("ref", nargs="?", default="",
                        help="記録番号(省くと最近の一覧)")
    parser.add_argument("--dir", action="append", default=[],
                        help="ログのフォルダ(下のフォルダも探す。何度でも指定可)")
    parser.add_argument("--days", type=int, default=7,
                        help="一覧で何日前まで見るか(既定 7)")
    parser.add_argument("--out", default="", help="まとめを書くファイル")
    args = parser.parse_args(argv)

    # このプログラム自身の出力が主役。業務ログを画面に混ぜない
    silence_console()
    folders = [Path(d) for d in args.dir] or None
    recursive = bool(args.dir)

    if not args.ref:
        rows = log_report.recent_errors(days=args.days, limit=200,
                                        folders=folders, recursive=recursive)
        if not rows:
            print(f"最近{args.days}日に記録番号の付いた記録はありません。")
            return 0
        for row in rows:
            pc = f"[{row['pc']}] " if row["pc"] else ""
            print(f"{row['at']}  {row['ref']}  {pc}{row['where']}  {row['what']}")
        print(f"\n{len(rows)}件。1件をまとめるには: "
              "python tools/log_report.py <記録番号> --dir <フォルダ>")
        return 0

    text = log_report.report(args.ref, folders=folders, recursive=recursive)
    if text is None:
        print(f"記録番号 {args.ref} の記録が見つかりません。", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8-sig")
        print(f"書きました: {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
