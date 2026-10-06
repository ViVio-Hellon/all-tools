-- 看板システム SQLite スキーマ
-- Access(.accdb) の 看板_<ライン> テーブル群を 1 つのテーブルに正規化して保持する。

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 取り込み元 Access テーブルの情報(書き戻しに必要)
CREATE TABLE IF NOT EXISTS line_source (
    line         TEXT PRIMARY KEY,
    table_name   TEXT NOT NULL,
    key_column   TEXT NOT NULL,
    key_category TEXT NOT NULL,   -- NUMBER / DATE / TEXT
    column_map   TEXT NOT NULL,   -- JSON: 論理名 -> Access の実列名
    categories   TEXT NOT NULL,   -- JSON: Access の実列名 -> 型カテゴリ
    source_path  TEXT NOT NULL,
    imported_at  TEXT NOT NULL
);

-- 看板の 1 行(資材 × サイズ)
--
-- dirty_columns について:
--   ローカル SQLite は各端末専用の写しであり、他端末の変更はこの端末が
--   気づかないうちに Access 側で進んでいることがある。そのため
--   「dirty な行は状態列(欲/不/更新日/発送/倉庫確認日時/保留/注文中日時)を丸ごと
--   Access へ送る/Access からの再取り込みで丸ごと保護する」という単純な
--   実装では、この端末が触っていない列(例: 発送)まで古い値で
--   上書きしてしまう(他端末の書き戻しを消してしまう)恐れがある。
--   dirty_columns にはこの端末が実際に変更を意図した列だけを記録し、
--   書き戻しはその列だけを送る・再取り込みはそれ以外の列だけを更新する
--   ことで、列単位の粒度で安全に保つ。
CREATE TABLE IF NOT EXISTS kanban_item (
    line          TEXT    NOT NULL,
    mgmt_no       TEXT    NOT NULL,            -- 管理番号
    material      TEXT    NOT NULL DEFAULT '', -- 資材
    size          TEXT    NOT NULL DEFAULT '', -- サイズ
    want          TEXT    NOT NULL DEFAULT '', -- 欲
    unwant        TEXT    NOT NULL DEFAULT '', -- 不
    ordered_at    TEXT    NOT NULL DEFAULT '', -- 更新日
    shipped       TEXT    NOT NULL DEFAULT '', -- 発送
    confirmed_at  TEXT    NOT NULL DEFAULT '', -- 倉庫確認日時
    permanent     TEXT    NOT NULL DEFAULT '', -- 常設品
    hold          TEXT    NOT NULL DEFAULT '', -- 保留(画面表示は「注文中」)
    hold_at       TEXT    NOT NULL DEFAULT '', -- 注文中日時(旧: 理由)
    row_order     INTEGER NOT NULL DEFAULT 0,  -- Access 上の並び順
    rev           INTEGER NOT NULL DEFAULT 1,  -- 楽観ロック用(更新ごとに +1)
    updated_at    TEXT    NOT NULL DEFAULT '',
    updated_by    TEXT    NOT NULL DEFAULT '', -- 更新した端末名
    dirty         INTEGER NOT NULL DEFAULT 0,  -- 1 = 共有DBへ未反映
    dirty_columns TEXT    NOT NULL DEFAULT '', -- 未反映の列名(カンマ区切り、論理名)
    sync_attempts INTEGER NOT NULL DEFAULT 0,
    sync_error    TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (line, mgmt_no)
);

CREATE INDEX IF NOT EXISTS idx_kanban_line_order ON kanban_item (line, row_order);
CREATE INDEX IF NOT EXISTS idx_kanban_dirty      ON kanban_item (dirty, line);

-- フォームの開閉状態(Access の Form状態管理 に対応)
--
-- **ここは観測の記録だけです。** 共有DBにそう書いてあった、というだけを
-- 持ちます。共有DBへ「開いています」と書くのは kanban/presence.py の心拍
-- だけで、手元には溜めません。
--
-- seen_at は「remote_stamp が変わったのを見た**こちらの**時刻」。
-- 相手の時計と比べないので、端末間の時刻ずれで「ずっと古い」ことに
-- なりません(相手が心拍を打ち続けるかぎり seen_at が更新される)。
--
-- status / updated_at / host / dirty / sync_attempts は**もう使っていません**。
-- 「手元に溜めて書き戻しのついでに送る」経路があった頃の名残です。同じマスに
-- 書き手が 2 つある形になるうえ、溜まっても「未反映 N 件」に出ない
-- (pending_count は kanban_item しか数えない)ため、外しました。
-- 列自体は既存ファイルとの互換のために残してあります(消しても誰も読まない)。
CREATE TABLE IF NOT EXISTS line_status (
    line          TEXT PRIMARY KEY,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    remote_status TEXT    NOT NULL DEFAULT '',
    remote_stamp  TEXT    NOT NULL DEFAULT '',
    remote_host   TEXT    NOT NULL DEFAULT '',
    seen_at       TEXT    NOT NULL DEFAULT '',
    -- 以下は未使用(上記のとおり)
    status        TEXT    NOT NULL DEFAULT '閉',
    updated_at    TEXT    NOT NULL DEFAULT '',
    host          TEXT    NOT NULL DEFAULT '',
    dirty         INTEGER NOT NULL DEFAULT 0,
    sync_attempts INTEGER NOT NULL DEFAULT 0
);

-- 端末間の排他(書き戻しを 1 台だけで走らせる等)
CREATE TABLE IF NOT EXISTS app_lock (
    name         TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

-- 操作履歴(トラブル時の追跡用)
CREATE TABLE IF NOT EXISTS operation_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    line      TEXT NOT NULL DEFAULT '',
    mgmt_no   TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    host      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_operation_log_at ON operation_log (at);

-- 看板の出来事(集計のための記録。kanban/domain/events.py)
--
-- 看板の表はいまの状態しか持たないので、「いつ出して・いつ届いて・次に
-- いつ出したか」は状態が変わった瞬間に記録するしかない。ボタンの操作と
-- 同じトランザクションで積み、書き戻しと一緒に共有DBの [看板履歴] へ送る
-- (sent = 1)。uid は共有DB側で二重に入れないための印
CREATE TABLE IF NOT EXISTS kanban_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT NOT NULL,
    at         TEXT NOT NULL,
    line       TEXT NOT NULL,
    mgmt_no    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    material   TEXT NOT NULL DEFAULT '',
    size       TEXT NOT NULL DEFAULT '',
    ordered_at TEXT NOT NULL DEFAULT '',
    host       TEXT NOT NULL DEFAULT '',
    sent       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kanban_event_sent ON kanban_event (sent, id);

-- 看板ごとのコメント(倉庫 ⇔ 現場のやり取り)。共有DBの [看板コメント] の手元の写し
--
-- 自分で書いたものは sent = 0 で積み、書き戻しと一緒に送る(出来事と同じ)。
-- 他の端末が書いたものは取り込みで受け取る(sent = 1)。uid で二重に入れない。
-- kind: 'コメント' / '片付け'。片付け = その看板が届いた(赤を消した)印で、
-- **それより前のコメントは画面から片付く**(共有DBには残り、集計の明細に載る)
CREATE TABLE IF NOT EXISTS kanban_comment (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    uid      TEXT NOT NULL UNIQUE,
    at       TEXT NOT NULL,
    line     TEXT NOT NULL,
    mgmt_no  TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'コメント',
    side     TEXT NOT NULL DEFAULT '',
    host     TEXT NOT NULL DEFAULT '',
    body     TEXT NOT NULL DEFAULT '',
    sent     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kanban_comment_item ON kanban_comment (line, mgmt_no, at);
CREATE INDEX IF NOT EXISTS idx_kanban_comment_sent ON kanban_comment (sent, id);

-- この端末でどこまで読んだか(端末ごと)。同じ側の端末と共有する既読は
-- kanban_comment の kind = '既読' の行(Store._share_read)。read_id は読んだときに
-- 出ていた最後のコメントの **この端末での** 番号(kanban_comment.id)。
-- 書いた日時で比べると、共有フォルダに届かない間に書かれて後から届いた
-- コメントや、時計のずれた端末のコメントを、開く前から「読んだ」ことにしてしまう
CREATE TABLE IF NOT EXISTS comment_read (
    line     TEXT NOT NULL,
    mgmt_no  TEXT NOT NULL,
    read_id  INTEGER NOT NULL,
    PRIMARY KEY (line, mgmt_no)
);
