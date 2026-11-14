"""SQLite 저장소. 표준 라이브러리 sqlite3만 사용한다."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("DORAN_DB", Path(__file__).resolve().parent.parent / "data" / "doran.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL UNIQUE,    -- 원문 링크
    source          TEXT NOT NULL,           -- 퍼온 곳 (TechCrunch, r/LocalLLaMA ...)
    kind            TEXT NOT NULL,           -- news | community | video | research
    origin          TEXT NOT NULL DEFAULT 'rss',  -- rss | hn | reddit | lobsters | devto | youtube | hf | github
    origin_id       TEXT,                    -- 그 플랫폼에서의 글 ID
    hn_id           TEXT,                    -- 연결된 Hacker News 토론
    hn_checked_at   INTEGER NOT NULL DEFAULT 0,
    rx_checked_at   INTEGER NOT NULL DEFAULT 0,
    bsky_checked_at INTEGER NOT NULL DEFAULT 0,
    published_at    INTEGER NOT NULL,
    collected_at    INTEGER NOT NULL,
    views           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at DESC);

-- 바깥(다른 플랫폼)에서 긁어온 반응
CREATE TABLE IF NOT EXISTS reactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform   TEXT NOT NULL,                -- hn | reddit | lobsters | bsky | masto | devto
    ext_id     TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    url        TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(platform, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id, created_at);

-- 도란도란 유저가 남긴 익명 댓글
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    nickname   TEXT NOT NULL,
    pw_hash    TEXT NOT NULL,
    ip_tag     TEXT NOT NULL DEFAULT '',     -- IP 앞 두 자리만 (예: 121.165)
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, created_at);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 나중에 붙은 컬럼. 이미 있는 DB(Actions 캐시)에도 붙여 준다.
MIGRATIONS = [
    ("posts", "title_ko", "TEXT"),
    ("posts", "summary_ko", "TEXT"),
    ("reactions", "body_ko", "TEXT"),
]


def init() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
