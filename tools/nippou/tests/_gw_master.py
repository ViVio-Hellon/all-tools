"""GW計算のテスト用に、梱包資材マスタ(資材重量・VC重量)を置く

【なぜ要るのか】
資材重量マスタが読めないとき、GW計算は**断ります**(VBA「資材重量なし」)。
以前は全部0で計算を続けていたので、マスタの無いテスト環境でも計算が
通っていました ── その「通っていた」は、パレット重量だけの軽いGWを
出していたということです。

計算を試すテストは、ここで**本物と同じ形のマスタ**を置いてから押します。
値は提出された `梱包資材マスタ.sqlite3` の「資材重量」と同じです
(取引先名などの個人に関わる値は入っていない表なので、そのまま使えます)。

**テストの一時フォルダの外へは書きません。**
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

#: (梱包資材名, 単位質量, 係数)。提出された資材重量マスタのうち、GW計算が
#: 引く行だけ
MATERIAL_ROWS: tuple[tuple[str, str, str], ...] = (
    ("ダンプレート", "0.250", "1"),
    ("外装紙", "0.0842", "1"),
    ("合紙", "0.023", "1"),
    ("ポリシート", "0.141", "1"),
    ("アングル", "0.336", "1"),
    # 縦バンドの角当て。**1個あたり**の重さ(個数で数える資材)
    ("縦バンドアングル", "0.05", "1"),
    ("ハードボード", "2.387", "1"),
    ("帯鉄シール無", "0.077", "1"),
    ("帯鉄シール有", "0.09", "1"),
    ("PETバンド", "0.026", "1"),
)

#: VC重量(品名, 単位質量, 係数)。テストで選ぶ品名だけ
VC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("テストVC", "0.05", "1"),
)

FILE_NAME = "梱包資材マスタ.sqlite3"


def write(ref_dir: Path, *, skip: tuple[str, ...] = ()) -> Path:
    """`ref_dir` に梱包資材マスタを置く。`skip` の資材名は**入れない**。

    「マスタにその名前が無い」を作るために `skip` があります。
    """
    ref_dir.mkdir(parents=True, exist_ok=True)
    path = ref_dir / FILE_NAME
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute('CREATE TABLE "資材重量" ("管理番号", "梱包資材名",'
                     ' "単位質量", "係数", "単位", "単位量", "備考")')
        conn.executemany(
            'INSERT INTO "資材重量" VALUES (?, ?, ?, ?, "Kg", "", "")',
            [(str(i), name, mass, coef)
             for i, (name, mass, coef) in enumerate(MATERIAL_ROWS, start=1)
             if name not in skip])
        conn.execute('CREATE TABLE "VC重量" ("管理番号", "品名", "単位質量", "係数")')
        conn.executemany('INSERT INTO "VC重量" VALUES (?, ?, ?, ?)',
                         [(str(i), *row) for i, row in enumerate(VC_ROWS, start=1)])
        conn.commit()
    finally:
        conn.close()
    return path


def _web_base():
    from tests._web import WebTestCase
    return WebTestCase


class GwWebTestCase(_web_base()):
    """GW計算を押すテストの土台。**マスタを置いてから始める。**"""

    def setUp(self) -> None:
        super().setUp()
        write(self.tmp / "ref")
