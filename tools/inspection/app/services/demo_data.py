"""模擬モード（--demo）用の見本フォルダを作る。Excel の中身は空で、一覧表示の確認用。"""
from __future__ import annotations

import os

DEMO_TREE = {
    "梱包": {
        "日常点検": ["フォークリフト始業点検.xlsx", "ラッピングマシン点検.xlsm", "台車点検.xlsx",
                   "コンベア日常点検.xlsx", "結束機点検.xlsm"],
        "月次点検": ["消火器点検.xlsx", "非常灯点検.xls", "安全帯点検.xlsx"],
        "週次点検": ["クレーン点検.xlsx", "パレット置場点検.xlsx"],
    },
    "押出": {
        "設備点検": ["プレス機始業点検.xlsm", "ビレットヒーター点検.xlsx", "冷却装置点検.xlsx"],
        "金型": ["金型受入点検.xlsx", "金型保管庫点検.xlsx"],
    },
    "品質": {
        "検査記録": ["寸法検査記録.xlsx", "外観検査記録.xlsx", "エラー確認用.xlsx"],
    },
    "NS1": {
        "巡回": ["巡回点検表.xlsx"],
    },
}
UNMATCHED = [("品質", "検査記録", "旧様式_検査記録.xlsx")]


def ensure_demo_folder(base_dir: str) -> str:
    root = os.path.join(base_dir, "demo_一括管理_点検表")
    for category, subs in DEMO_TREE.items():
        for sub, files in subs.items():
            folder = os.path.join(root, category, sub)
            os.makedirs(folder, exist_ok=True)
            for name in files:
                _touch(os.path.join(folder, f"{category}_{sub}_{name}"))
    for category, sub, name in UNMATCHED:
        _touch(os.path.join(root, category, sub, name))
    return root


def _touch(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "wb"):
            pass
