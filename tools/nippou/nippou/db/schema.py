"""SQLite schema for the local day-to-day store.

Design note: the original Access side used one pair of tables *per
production line* (``T_日報ヘッダー_<line>`` / ``T_日報明細_<line>``,
see ``NippouDB_HeaderTable`` / ``NippouDB_DetailTable``) because Access
table names couldn't easily be parameterised by a WHERE clause the same
way a normal RDBMS query can. SQLite has no such limitation, so here we
use a single normalized ``daily_header`` / ``daily_detail`` pair with
``line`` as an ordinary column -- functionally identical, simpler to
query, and the access_bridge layer re-expands it into the original
per-line table naming when pushing to Access (see
``access_bridge/script_gen.py``) so the .accdb side stays compatible with
the legacy schema.
"""
from __future__ import annotations

DDL = """
CREATE TABLE IF NOT EXISTS daily_header (
    report_date            TEXT NOT NULL,
    line                    TEXT NOT NULL,
    shift                   TEXT NOT NULL,
    page                    INTEGER NOT NULL,
    worker                  TEXT,
    day_shift               TEXT,
    count                   TEXT,
    weight_kg               TEXT,
    lot_count                TEXT,
    coefficient_lot_count   TEXT,
    reason                   TEXT,
    saved_at                 TEXT NOT NULL,
    dirty                    INTEGER NOT NULL DEFAULT 1,
    synced_at                 TEXT,
    -- いま入っている中身の指紋と、**共有へ出したときの**指紋
    -- (`logic/fingerprint.page_fingerprint`)。
    --
    -- 2つ持つのは「送ったあとに直して、また元に戻した」を拾うため
    -- です ── 戻せば指紋も戻るので、送り直しは要りません。
    -- 同じなら `dirty` を立てない、が保存の決まりです(README v3.65.0)
    content_hash              TEXT,
    synced_hash               TEXT,
    PRIMARY KEY (report_date, line, shift, page)
);

CREATE TABLE IF NOT EXISTS daily_detail (
    report_date TEXT NOT NULL,
    line        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    page        INTEGER NOT NULL,
    row_no      INTEGER NOT NULL,
    lot TEXT, zai TEXT, siz TEXT, ken TEXT, kz TEXT, kh TEXT, sz TEXT, sh TEXT,
    hit TEXT, ai TEXT, mai TEXT, tut TEXT, vc TEXT, et TEXT, s TEXT, th TEXT,
    ss TEXT, ths TEXT, sth TEXT, tht TEXT, con TEXT, wei TEXT, tim TEXT, uni TEXT,
    others1 TEXT, others2 TEXT, others3 TEXT, others4 TEXT, others5 TEXT, others6 TEXT,
    keisu TEXT,
    -- その行の理由。**行ごとに持ちます。**
    --
    -- 紙の「ヨ：その他（理由を記載）」は欄が1つしかなく、VBA もそれに
    -- 合わせてページに1つ(`daily_header.reason`)でした。フォーマットの都合で
    -- そうなっていただけなので、DBは行ごとに持ちます ── 停止理由で
    -- 「その他」を選ぶたび、その行の理由として残ります。
    -- 紙と共有へ出すときは「3行目: ○○ / 7行目: ××」の形で1つにまとめます
    -- (`logic/reasons.combine`)
    reason TEXT,
    -- 選んだ引当番号。**この端末の中だけで持ちます**(共有の日報管理へは
    -- 送りません)。紙にも Access にも引当番号の欄は無く、あれば足りる
    -- ものではなく「どの引当で埋めたか」を後から見るための控えです
    hiki_no TEXT,
    -- Gコースのロットだった印(GSS / GSS/製造)。これも**この端末の中だけ**。
    -- 寸法が BOX最終実績から来たことを、開き直しても寸法の欄に出すため(v4.17.0)
    box_course TEXT,
    PRIMARY KEY (report_date, line, shift, page, row_no),
    FOREIGN KEY (report_date, line, shift, page)
        REFERENCES daily_header (report_date, line, shift, page)
        ON DELETE CASCADE
);

-- ==================================================================
-- 集計フォーマット (VBA の「集計シート」) ── **値で残す**
--
-- 上の2つ(`daily_header` / `daily_detail`)が**印刷フォーマット**で、
-- 打った内容そのものです。ここから下の3つは、VBA の `Agg_OutPut` が
-- 集計シートへ並べ直していたものを**表にしたもの**です。
--
--     daily_header/daily_detail  打つところ    (印刷フォーマット)
--              ↓ 保存のたびに作り直す (`services/summary.py`)
--     packing_report             1作業日1ライン1直
--     packing_report_detail      1ロット1行
--     packing_stop_detail        1停止1行 (**横持ちから縦持ちへ**)
--
-- **打つ場所は1つだけです。** こちらは投影なので、食い違いようが
-- ありません(食い違ったら、投影を作り直せば必ず揃います)。
--
-- 明細から計算できる値をあえて置くのは、
--   ・そのとき使った負荷係数・停止の分類で**固定して残す**ため
--     (マスタが差し替わっても、当時の集計は当時のまま)
--   ・期間のグラフが、何百ページぶんの明細を読み直さずに出せるため
--   ・**ツールを通さずに読める表**になるため
-- です。
--
-- VBA の集計シートは座標で意味が決まっていました(`Cells(n, 25)` が
-- 実績枚数、など)。**座標は持ち込みません** ── 列の意味は名前で
-- 決まります。
-- ==================================================================

-- 1作業日・1ライン・1直で1行。VBA `Aggre_Calcul` が集計シートの
-- 上の帯へ書いていた値
CREATE TABLE IF NOT EXISTS packing_report (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date               TEXT NOT NULL,
    line_name               TEXT NOT NULL,
    shift                   TEXT NOT NULL,      -- 日勤 / 1直 / 2直 / 3直
    worker_name             TEXT,
    daytime_operation       TEXT,               -- 昼稼働(有/無)
    reason                  TEXT,               -- 作業コメント(紙の「理由」)
    pages                   INTEGER NOT NULL DEFAULT 0,
    operation_time          REAL,               -- 操業時間 (Opetional)
    work_time               REAL,               -- 作業時間合計 (WorkT)
    operating_time          REAL,               -- 稼働時間 (OpeTime)
    operating_rate          REAL,               -- 稼働率 (OpeRate)
    equipment_stop_total    REAL,               -- 管理ロス設備停止 (StopM)
    setup_stop_total        REAL,               -- 段取り・突発停止 (StopH)
    handling_stop_total     REAL,               -- ハンドリング停止
    total_lot_count         INTEGER,            -- ﾛｯﾄ数(重複を除く)
    coefficient_lot_count   REAL,               -- 係数処理ﾛｯﾄ数
    total_quantity          REAL,               -- 合計枚数
    total_weight            REAL,               -- 合計重量(Kg)
    productivity            REAL,               -- 生産性(t/h)
    dirty                   INTEGER NOT NULL DEFAULT 1,
    synced_at               TEXT,
    -- 日報と同じ指紋のしくみ(`logic/fingerprint.summary_fingerprint`)。
    -- 集計は保存のたびに作り直しますが、**中身が同じなら送り直しません**
    content_hash            TEXT,
    synced_hash             TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    -- 同じ作業日・ライン・直を二重に持たない(再保存は入れ替え)
    UNIQUE (work_date, line_name, shift)
);

-- 1ロット1行。VBA `Agg_OutPut` が集計シートの 8〜151行目へ並べていたもの。
-- **紙に載らない欄(用途コード・用途名・納入先・包装仕様書No・コイル
-- 縦割/横縦割)もここに入ります** ── 集計だけが残す場所だったので
CREATE TABLE IF NOT EXISTS packing_report_detail (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id               INTEGER NOT NULL,
    -- どのページの何行目から来たか。**数が合わないときに元の行へ戻れる**
    page                    INTEGER NOT NULL DEFAULT 1,
    row_no                  INTEGER NOT NULL DEFAULT 0,
    lot_no                  TEXT,
    material_condition      TEXT,               -- 材・調質
    dimension               TEXT,               -- 厚×幅×丈
    incoming_quantity       REAL,               -- 検入枚数
    start_hour              INTEGER,
    start_minute            INTEGER,
    end_hour                INTEGER,
    end_minute              INTEGER,
    worker_count            REAL,               -- 作業人数
    -- 合紙。紙は「有 / 無」の2択なので TEXT にしてあります
    interleaf               TEXT,
    packing_quantity        REAL,               -- 個装単位 枚数
    packing_package_count   REAL,               -- 梱包単位 包数
    vc_type                 TEXT,
    actual_quantity         REAL,               -- 実績枚数
    actual_weight           REAL,               -- 実績重量(Kg)
    work_time               REAL,               -- 作業時間(分)
    unit_weight             REAL,               -- 単重
    coefficient_lot_count   REAL,               -- 係数処理ﾛｯﾄ数
    purpose_code            TEXT,               -- 用途コード
    purpose_name            TEXT,               -- 用途名
    delivery_destination    TEXT,               -- 納入先
    packing_spec_no         TEXT,               -- 包装仕様NO
    etc                     TEXT,               -- 反転・EX etc
    coil_vertical_split     TEXT,               -- コイル縦割
    coil_horizontal_split   TEXT,               -- コイル横縦割
    hiki_no                 TEXT,               -- 引当番号(この端末だけの控え)
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES packing_report (id) ON DELETE CASCADE
);

-- 1停止1行。シートでは作業停止①②③が横に並んでいましたが、縦にします
-- ── 4つ目が要るようになっても列を増やさずに済み、「記号ごとに足す」が
-- 素直な集計になるため
CREATE TABLE IF NOT EXISTS packing_stop_detail (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detail_id     INTEGER NOT NULL,
    stop_no       INTEGER NOT NULL,     -- 1〜3(紙の①②③)
    stop_code     TEXT,                 -- 停止記号
    stop_reason   TEXT,                 -- 内訳名(マスタが読めたとき)
    -- 管理ロス停止 / 突発停止 / ハンドリング停止。**そのときの分類で
    -- 固定して残す** ── 記号の付け替えで去年のグラフが書き換わらない
    stop_kind     TEXT,
    stop_minutes  REAL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (detail_id) REFERENCES packing_report_detail (id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS shift_config (
    shift_key   TEXT PRIMARY KEY,   -- '1','2','3','昼'
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS print_status (
    shift_name  TEXT NOT NULL,
    print_date  TEXT NOT NULL,   -- ISO yyyy-mm-dd (business date, TodayCheck-equivalent)
    line        TEXT NOT NULL,
    printed_at  TEXT NOT NULL,
    PRIMARY KEY (shift_name, print_date, line)
);

-- 直の引き継ぎ ── **前の直が片付かないまま次の直が進むのを止める関門**
--
-- 【なぜ表に残すのか】
-- 押し忘れ・打ち間違いのまま直の時間が過ぎ、次の直がそのまま入力を
-- 進めてしまう ── これが一番起きていた流れです。次の直の人は
-- 「共有へ保存」は押せますが、**前の直の間違いは直せません**
-- (運用でそう決まっています)。
--
-- そこで、直せないまま次へ進むときに **誰が・いつ・何を残したまま
-- 引き継いだか**をここへ書きます。画面の合図だけにすると、閉じた
-- 時点で消えてしまい、翌月の集計まで誰も気づきません。
--
-- 消しません。**その直が共有へ出たかどうかは `daily_header.dirty` が
-- 持っている**ので、ここは「引き継いだ事実」の記録だけです。
CREATE TABLE IF NOT EXISTS shift_handover (
    report_date TEXT NOT NULL,   -- 引き継がれた側(前の直)
    line        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    taken_by    TEXT NOT NULL,   -- 引き継いだ人(次の直の作業者)
    taken_at    TEXT NOT NULL,
    findings    TEXT NOT NULL DEFAULT '',  -- 引き継いだ時点で残っていた断り
    PRIMARY KEY (report_date, line, shift)
);

-- 月替わりの書き出しを済ませた月 ── **同じ月を何度も書き出さない**
--
-- 【なぜ要るのか】
-- VBA は書き出したあと、その月のシートを消していました。次の月替わりの
-- 判定は残っているシートを見るので、書き出した月は二度と候補に
-- 上がりません。このツールは**手元の日報を消さない**ので、覚えておかないと
-- いちばん古い月がいつまでも候補に残ります:
--
--     9月の共有へ保存のたびに 8月をまた書き出す
--     10月になっても 8月を書き出し、**9月はいつまでも書き出されない**
--
-- 書き出したあとに、その月の日報の**中身が変わっていれば**(直した・
-- 取り込んだ)書き出し直します。見るのは時刻ではなく中身の指紋
-- (`content_hash` をその月ぶん束ねたもの)── 同じ中身を保存し直した
-- だけなら出し直さず、1秒のうちに直しても見落としません。
CREATE TABLE IF NOT EXISTS month_rollover (
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    done_at      TEXT NOT NULL,      -- 書き出しを済ませた時刻(ISO・人が読む用)
    fingerprint  TEXT NOT NULL,      -- 書き出した時点の、その月の中身の指紋
    PRIMARY KEY (year, month)
);

-- 共有保存の履歴 ── **各直で「共有へ保存」を押した時刻と担当者**
--
-- 押すたびに、送った直ごとに1行ずつ残します(送るものが無かった・関門で
-- 止めた、も残す ── 押したこと自体が記録したいことなので)。
--
-- 共有の `日報データ.sqlite3` にも同じ行を `T_共有保存履歴` として写します
-- (`services/push_history`)。`shared` は写し終えたかの印で、共有に
-- 届かなかった行は次の「共有へ保存」でまとめて写します。
CREATE TABLE IF NOT EXISTS push_history (
    id            TEXT PRIMARY KEY,   -- 端末をまたいでも重ならない番号(uuid)
    pressed_at    TEXT NOT NULL,      -- 押した日時(ISO)
    report_date   TEXT NOT NULL,      -- 送った直(報告日・ライン・直)
    line          TEXT NOT NULL,
    shift         TEXT NOT NULL,
    pages         INTEGER NOT NULL DEFAULT 0,   -- 送った(送ろうとした)ページ数
    worker        TEXT NOT NULL DEFAULT '',     -- その直の担当者(作業者名)
    result        TEXT NOT NULL,                -- 送れた / 送るもの無し / 関門で止めた / …
    failed_pages  INTEGER NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '',     -- 送れなかった・止めた理由
    pressed_key   TEXT NOT NULL DEFAULT '',     -- 押したときの直(「2026年9月25日 2直」)
    pressed_by    TEXT NOT NULL DEFAULT '',     -- 押したときの直の担当者
    via           TEXT NOT NULL DEFAULT '',     -- 逃げ道の一文で通したとき、その文
    terminal      TEXT NOT NULL DEFAULT '',     -- 押した端末(PC名)
    shared        INTEGER NOT NULL DEFAULT 0    -- 共有の T_共有保存履歴 へ写したか
);

-- 後日作成 ── **終わった直を、あとから管理者が作った**という事実 (v4.0.0)
--
--     過去分を作ることはある一定で仕方がない場面はある
--     ・管理者であること ・現在分とぶつからない ・抜けた部分に保存される
--     ・後日作ったことがわかる
--
-- 作る口は「記録を見る → 日付を指定して見る → この直を後から作る」だけ
-- (`logic/backfill.py` が通すかを決める)。ここは**作り始めた事実**の記録で、
-- 中身は `daily_header` / `daily_detail` に入ります。印を出すのは**中身が
-- 保存されている枠だけ**(作りかけて何も打たずに戻った枠は、何も無いのと同じ)。
-- 消しません ── 後から作ったのは過去の出来事です。
CREATE TABLE IF NOT EXISTS backfill (
    report_date TEXT NOT NULL,
    line        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    page        INTEGER NOT NULL,
    opened_at   TEXT NOT NULL,              -- 後から作り始めた日時(ISO)
    terminal    TEXT NOT NULL DEFAULT '',   -- 作った端末(PC名)
    note        TEXT NOT NULL DEFAULT '',   -- なぜ後から作ったか(任意)
    PRIMARY KEY (report_date, line, shift, page)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT NOT NULL,   -- 'import' | 'push'
    table_name   TEXT,
    status       TEXT NOT NULL,   -- 'success' | 'error'
    detail       TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_header_dirty ON daily_header (dirty);
-- 標準作業時間へ、まだ写していない直 (`services/standard_time`)
--
-- 「共有へ保存」で送れた直は、共有の `標準作業時間.sqlite3` に実績として
-- 写します。共有が開けなかったときはここに残り、次の「共有へ保存」で
-- まとめて写します(写せたら消す)。
-- 控え(LocalBackup)へ写す待ちのページ(v4.12.0、`services/local_backup`)。
-- **保存・送れた印・消したときに、取引の中で入れます。** 写せたら消します。
-- 控えの置き場所に届かないあいだは溜まり、届いた次の保存か起動で写ります
CREATE TABLE IF NOT EXISTS backup_pending (
    report_date TEXT NOT NULL,
    line        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    page        INTEGER NOT NULL,
    queued_at   TEXT NOT NULL,
    PRIMARY KEY (report_date, line, shift, page)
);

CREATE TABLE IF NOT EXISTS standard_time_pending (
    work_date   TEXT NOT NULL,
    line        TEXT NOT NULL,
    shift       TEXT NOT NULL,
    queued_at   TEXT NOT NULL,
    PRIMARY KEY (work_date, line, shift)
);

CREATE INDEX IF NOT EXISTS idx_push_history_shared ON push_history (shared);
CREATE INDEX IF NOT EXISTS idx_push_history_line_date ON push_history (line, report_date);
CREATE INDEX IF NOT EXISTS idx_daily_header_line_date ON daily_header (line, report_date);
CREATE INDEX IF NOT EXISTS idx_packing_report_line_date
    ON packing_report (line_name, work_date);
CREATE INDEX IF NOT EXISTS idx_packing_report_dirty ON packing_report (dirty);
CREATE INDEX IF NOT EXISTS idx_packing_detail_report
    ON packing_report_detail (report_id);
CREATE INDEX IF NOT EXISTS idx_packing_detail_lot
    ON packing_report_detail (lot_no);
CREATE INDEX IF NOT EXISTS idx_packing_stop_detail
    ON packing_stop_detail (detail_id);
"""

# 役目を終えた表。**中身は投影(明細から作り直せるもの)** なので、
# 消しても打った日報は1つも失われません。
#
#   daily_summary / daily_stop_summary
#       `packing_report` と `packing_stop_detail` に置き換わりました。
#       直の合計は `packing_report`、記号ごとの停止は
#       `packing_stop_detail` を記号でまとめたもの(`stop_rollup`)。
#       同じ数字を2か所に置くと、**片方だけ古くなったときに
#       どちらが正か決められません。**
DROPPED_TABLES: tuple[str, ...] = ("daily_summary", "daily_stop_summary")


# あとから足した列。**すでに動いている端末のDBには `CREATE TABLE IF NOT
# EXISTS` が効かない**ので、無ければ足す。
#   (表名, 列名, 型)
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("daily_detail", "hiki_no", "TEXT"),
    ("daily_detail", "box_course", "TEXT"),       # v4.17.0 Gコースの印
    ("daily_detail", "reason", "TEXT"),
    # 指紋。**空のまま足します** ── すでに送ってあるページは
    # `synced_hash` が空なので、次の保存で1度だけ未送信に戻ります。
    # そこで送れば指紋が揃い、以後は同じ中身なら立ちません
    ("daily_header", "content_hash", "TEXT"),
    ("daily_header", "synced_hash", "TEXT"),
    ("packing_report", "content_hash", "TEXT"),
    ("packing_report", "synced_hash", "TEXT"),
)


def ensure_schema(conn) -> None:
    """表をそろえる。**新しい端末でも、SQLを手で流す必要はありません。**

    `CREATE TABLE IF NOT EXISTS` なので、すでにある表と中身には触れません。
    あとから足した列は `_add_missing_columns` が継ぎ足します。
    """
    conn.executescript(DDL)
    _add_missing_columns(conn)
    _drop_replaced_tables(conn)
    _rename_old_lines(conn)
    conn.commit()


#: 手元のDBの版(`PRAGMA user_version`)。1 = ラインの名前を正規の呼び名へ揃えた(v4.13.0)
LINE_NAMES_VERSION = 1

#: ラインを持つ列の名前(表ごとに違う。`line` と `line_name`)
LINE_COLUMNS: tuple[str, ...] = ("line", "line_name")


def _rename_old_lines(conn) -> None:
    """v4.12 までの名前(`LS` `MARU` …)で入っている行を、正規の呼び名へ揃える(v4.13.0)。

    **1度だけ**です(`PRAGMA user_version` に印を付ける ── 要求のたびに開くので、
    毎回すべての表を見に行かない)。ふつうは何もありません(運用前なので)。試しに
    動かした端末の手元の日報が、ラインの名前が変わって見えなくなるのを防ぐためです。

    ラインを持つ列(`LINE_COLUMNS`)のある表を、**表の名前を決め打ちせずに**探します。
    同じキーの行がもう正規の呼び名である(あり得ないはずの)ときは、その行は残します
    (`UPDATE OR IGNORE` ── 起動を止めない)。明細は見出しを参照しているので、揃えるあいだ
    だけ外部キーの確かめを外します(見出しと明細を同じ名前へ揃えるので、終われば合う)。
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= LINE_NAMES_VERSION:
        return
    from ..logic import line_names

    renamed = line_names.renamed()
    keys_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if conn.in_transaction:
        conn.commit()                     # foreign_keys はトランザクションの外でしか効かない
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            for column in LINE_COLUMNS:
                if column not in columns:
                    continue
                for d in renamed:
                    conn.execute(f'UPDATE OR IGNORE "{table}" SET "{column}"=? WHERE "{column}"=?',
                                 (d.official, d.old))
        conn.execute(f"PRAGMA user_version={LINE_NAMES_VERSION}")
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if keys_on else 'OFF'}")


def _drop_replaced_tables(conn) -> None:
    """置き換わった**投影の表**を落とす。

    落とすのは、明細から何度でも作り直せる表だけです(`DROPPED_TABLES`
    の注記を参照)。打った日報(`daily_header` / `daily_detail`)には
    触れません。
    """
    for table in DROPPED_TABLES:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')


def _add_missing_columns(conn) -> None:
    """後から足した列を継ぎ足す。

    `CREATE TABLE IF NOT EXISTS` は**表があれば何もしません。** 列を
    増やしたときに既存の端末へ届かないので、ここで1つずつ確かめて
    足します。`ALTER TABLE ... ADD COLUMN` は既定値 NULL で足すだけなので、
    入っているデータには触れません。
    """
    for table, column, kind in ADDED_COLUMNS:
        names = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not names:
            continue                      # その表がまだ無い(DDLが作る)
        if column in names:
            continue
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {kind}')
