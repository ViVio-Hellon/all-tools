"""送らずに取りやめた登録を、その端末の人に知らせる

【何が起きていたか】
同じ人・同じ日の休みを2台でほぼ同時に登録すると、取り込み元へ後から
届いたほうは**送らずに取りやめます**(二重登録を防ぐ ``business_rules``)。
取りやめ自体は正しいのですが、**後から届いた側の画面には「登録しました」と
出たまま**で、知らせは何もありませんでした。次の取り込みで、画面は先に
届いた側の内容に入れ替わります。

先に届いた側と**直や繋ぎが違っていれば、こちらで選んだ内容は黙って
消えた**ことになります(2台で同時に操作して確かめたときに見つかった)。

【どうするか】
取りやめた登録を手元(``_sync_meta``)に覚えておき、その端末の画面に
出します。**確かめたと押されるまで出し続けます**(取り込み元へは
書けない ── 書きたい相手は他の端末の登録なので)。
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any

from .. import config, db

#: 覚えておく鍵(手元の ``_sync_meta``)
META_KEY = "skipped_duplicates"
#: 覚えておく上限。溜まり続けないように、古いものから捨てる
MAX_KEEP = 30


def remember(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """取りやめた登録を覚える。**すぐ書き込む**(送信が途中で失敗しても残す)。"""
    if not rows:
        return
    kept = pending(conn)
    now = _dt.datetime.now().strftime("%Y/%m/%d %H:%M")
    for row in rows:
        kept.append({
            "date": str(row.get("日付", "")),
            "name": str(row.get("登録内容", "")),
            "code": str(row.get("識別コード", "")),
            "shift": str(row.get("直", "")),
            "at": now,
        })
    db.set_meta(conn, META_KEY, json.dumps(kept[-MAX_KEEP:], ensure_ascii=False))
    conn.commit()


def pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """まだ確かめられていない、取りやめた登録。"""
    try:
        value = json.loads(db.get_meta(conn, META_KEY, "[]") or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def pending_at(path: str) -> list[dict[str, Any]]:
    """手元のDBファイルから読む(帯が数秒ごとに読むので、軽く1文だけ)。"""
    try:
        conn = sqlite3.connect(path, timeout=2)
    except sqlite3.Error:
        return []
    try:
        row = conn.execute('SELECT "value" FROM "_sync_meta" WHERE "key"=?',
                           (META_KEY,)).fetchone()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    try:
        value = json.loads(row[0]) if row and row[0] else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def acknowledge(conn: sqlite3.Connection) -> int:
    """確かめた。消した件数を返す。"""
    count = len(pending(conn))
    db.set_meta(conn, META_KEY, "[]")
    conn.commit()
    return count


def describe(items: list[dict[str, Any]]) -> dict[str, Any]:
    """画面に出す文言。**組み立てはここ**(画面は写すだけ)。"""
    if not items:
        return {"count": 0, "title": "", "text": ""}
    lines = []
    for item in items:
        try:
            day = _dt.datetime.strptime(item["date"], config.DATE_KEY_FORMAT)
            when = f"{day.month}月{day.day}日"
        except (KeyError, ValueError):
            when = str(item.get("date", ""))
        shift = f"({item['shift']}直)" if item.get("shift") else ""
        lines.append(f"・{when} {item.get('name', '')}さんの休み{shift}")
    text = ("次の登録は、**別の端末が先に同じ人・同じ日を登録していた**ため、"
            "取り込み元へ送りませんでした。\n\n" + "\n".join(lines) + "\n\n"
            "いまカレンダーに出ているのは、先に届いた登録です。"
            "直や繋ぎが違う場合は、その日の登録を確かめて、削除してから登録し直してください。")
    return {"count": len(items), "title": "送らなかった登録があります",
            "text": text.replace("**", "")}
