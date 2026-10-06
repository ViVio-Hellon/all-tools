-- VC計算マスタ (vc_master.sqlite3)
--
-- VBA に直書きされていた値を表に出したもの(docs/VBA解析.md §11)。
-- 行は SQLite の rowid で指す。品種の結びつきは「品種名」の文字で持ち、
-- 名前を直せば子の行もついてくる(ON UPDATE CASCADE)。親を消すときは
-- 子が残っていれば断る(ON DELETE RESTRICT)── 黙って消さない。
--
-- 共有フォルダに置くので **WAL にはしない**(WAL は共有メモリを使い、
-- SMB の上では開けない)。journal_mode は書くたびに DELETE を確かめる。

CREATE TABLE IF NOT EXISTS "VC品種" (
    "表示順"   INTEGER NOT NULL DEFAULT 0,
    "品種名"   TEXT    NOT NULL UNIQUE CHECK (length(trim("品種名")) > 0),
    "業者名"   TEXT    NOT NULL DEFAULT '',
    "VC厚"     REAL    NOT NULL CHECK ("VC厚" > 0),
    "内径"     REAL             CHECK ("内径" IS NULL OR "内径" > 0),
    "有効"     INTEGER NOT NULL DEFAULT 1 CHECK ("有効" IN (0, 1)),
    "備考"     TEXT,
    "更新日時" TEXT
);

CREATE TABLE IF NOT EXISTS "VC内径選択肢" (
    "品種名"   TEXT    NOT NULL
               REFERENCES "VC品種"("品種名") ON UPDATE CASCADE ON DELETE RESTRICT,
    "表示順"   INTEGER NOT NULL DEFAULT 0,
    "表示名"   TEXT    NOT NULL CHECK (length(trim("表示名")) > 0),
    "内径"     REAL    NOT NULL CHECK ("内径" > 0),
    "更新日時" TEXT,
    UNIQUE ("品種名", "内径")
);

CREATE TABLE IF NOT EXISTS "枚数定尺" (
    "表示順"   INTEGER NOT NULL DEFAULT 0,
    "記号"     TEXT    NOT NULL UNIQUE CHECK (length(trim("記号")) > 0),
    "表示名"   TEXT    NOT NULL DEFAULT '',
    "長さmm"   REAL    NOT NULL CHECK ("長さmm" > 0),
    "有効"     INTEGER NOT NULL DEFAULT 1 CHECK ("有効" IN (0, 1)),
    "更新日時" TEXT
);

-- 早見表の枠。「計算品種」がある枠は、その品種の VC厚 でブックの式から長さを出す
-- (VC厚 を2か所に持たない)。無い枠(R575B)は 早見表値.長さ をそのまま出す
CREATE TABLE IF NOT EXISTS "早見表ブロック" (
    "表示順"   INTEGER NOT NULL DEFAULT 0,
    "品種名"   TEXT    NOT NULL UNIQUE CHECK (length(trim("品種名")) > 0),
    "業者名"   TEXT    NOT NULL DEFAULT '',
    "枠色"     TEXT    NOT NULL DEFAULT 'navy',
    "有効"     INTEGER NOT NULL DEFAULT 1 CHECK ("有効" IN (0, 1)),
    "更新日時" TEXT,
    "計算品種" TEXT    REFERENCES "VC品種"("品種名") ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS "早見表値" (
    "品種名"   TEXT    NOT NULL
               REFERENCES "早見表ブロック"("品種名") ON UPDATE CASCADE ON DELETE RESTRICT,
    "内径"     REAL    NOT NULL CHECK ("内径" > 0),
    "肉厚"     REAL    NOT NULL CHECK ("肉厚" > 0),
    "長さ"     INTEGER          CHECK ("長さ" IS NULL OR "長さ" >= 0),  -- 計算品種の無い枠だけ使う
    "更新日時" TEXT,
    UNIQUE ("品種名", "内径", "肉厚")
);

CREATE TABLE IF NOT EXISTS "アプリ設定" (
    "キー"     TEXT PRIMARY KEY,
    "値"       TEXT NOT NULL DEFAULT '',
    "説明"     TEXT NOT NULL DEFAULT '',
    "更新日時" TEXT
);

-- 誰がいつ何を直したか。マスタ管理が書くたびに同じ取引の中で1行足す
CREATE TABLE IF NOT EXISTS "変更履歴" (
    "日時"     TEXT NOT NULL,
    "端末"     TEXT NOT NULL DEFAULT '',
    "ユーザー" TEXT NOT NULL DEFAULT '',
    "表"       TEXT NOT NULL,
    "操作"     TEXT NOT NULL,
    "行"       INTEGER,
    "変更前"   TEXT,
    "変更後"   TEXT
);

-- 画面に出さない内部の値(版・更新番号・管理者パスワードの撹拌値)
CREATE TABLE IF NOT EXISTS "_メタ" (
    "キー" TEXT PRIMARY KEY,
    "値"   TEXT NOT NULL DEFAULT ''
);
