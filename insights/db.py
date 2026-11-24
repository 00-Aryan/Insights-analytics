"""SQLite storage. Snapshots are append-only so we can see how a post grows over time."""
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    platform      TEXT NOT NULL,
    content_id    TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    format        TEXT,          -- short | long | reel | image | carousel | video
    duration_s    REAL,
    posted_at     TEXT NOT NULL, -- ISO 8601 UTC
    topic         TEXT,          -- owner label, e.g. "studio-tour"
    hook          TEXT,          -- owner label, e.g. "question", "before-after"
    decision      TEXT,          -- owner's call: push | hold | experiment
    decision_note TEXT,
    decided_at    TEXT,
    PRIMARY KEY (platform, content_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    platform         TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    hours_since_post REAL NOT NULL,
    views            INTEGER,
    reach            INTEGER,
    likes            INTEGER,
    comments         INTEGER,
    shares           INTEGER,
    saves            INTEGER,
    follows          INTEGER,     -- subscribersGained on YouTube
    avg_view_pct     REAL,        -- YouTube averageViewPercentage
    avg_watch_s      REAL,        -- seconds watched on average
    skip_rate        REAL,        -- Instagram reels_skip_rate (% skipped in first 3s)
    PRIMARY KEY (platform, content_id, fetched_at)
);
CREATE TABLE IF NOT EXISTS geo (
    platform   TEXT NOT NULL,
    content_id TEXT NOT NULL,     -- '_account' for account-level audience
    fetched_at TEXT NOT NULL,
    breakdown  TEXT NOT NULL,     -- country | city
    key        TEXT NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY (platform, content_id, breakdown, key)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    platform   TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    followers  INTEGER,
    PRIMARY KEY (platform, fetched_at)
);
CREATE TABLE IF NOT EXISTS comments (
    platform     TEXT NOT NULL,
    comment_id   TEXT NOT NULL,
    content_id   TEXT NOT NULL,
    text         TEXT,
    published_at TEXT,
    like_count   INTEGER,
    replied      INTEGER DEFAULT 0,
    PRIMARY KEY (platform, comment_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    api_calls   INTEGER,
    posts       INTEGER,
    error       TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hours_between(start_iso: str, end_iso: str) -> float:
    a = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return round((b - a).total_seconds() / 3600, 2)


@dataclass
class Post:
    platform: str
    content_id: str
    posted_at: str
    title: str | None = None
    url: str | None = None
    format: str | None = None
    duration_s: float | None = None


@dataclass
class Snapshot:
    platform: str
    content_id: str
    fetched_at: str
    hours_since_post: float
    views: int | None = None
    reach: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    follows: int | None = None
    avg_view_pct: float | None = None
    avg_watch_s: float | None = None
    skip_rate: float | None = None


@dataclass
class Comment:
    platform: str
    comment_id: str
    content_id: str
    text: str
    published_at: str | None = None
    like_count: int | None = None


@dataclass
class FetchResult:
    posts: list[Post] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    geo: list[tuple] = field(default_factory=list)  # (content_id, breakdown, key, value)
    followers: int | None = None
    api_calls: int = 0
    notes: list[str] = field(default_factory=list)


def connect(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save(conn: sqlite3.Connection, platform: str, result: FetchResult) -> None:
    now = utcnow()
    with conn:
        for p in result.posts:
            # Update platform fields but never overwrite the owner's labels/decisions.
            conn.execute(
                """INSERT INTO posts (platform, content_id, title, url, format, duration_s, posted_at)
                   VALUES (:platform, :content_id, :title, :url, :format, :duration_s, :posted_at)
                   ON CONFLICT(platform, content_id) DO UPDATE SET
                     title=excluded.title, url=excluded.url, format=excluded.format,
                     duration_s=excluded.duration_s, posted_at=excluded.posted_at""",
                asdict(p),
            )
        for s in result.snapshots:
            d = asdict(s)
            cols = ", ".join(d)
            conn.execute(
                f"INSERT OR REPLACE INTO snapshots ({cols}) VALUES ({', '.join(':' + k for k in d)})", d
            )
        for c in result.comments:
            conn.execute(
                """INSERT INTO comments (platform, comment_id, content_id, text, published_at, like_count)
                   VALUES (:platform, :comment_id, :content_id, :text, :published_at, :like_count)
                   ON CONFLICT(platform, comment_id) DO UPDATE SET
                     text=excluded.text, like_count=excluded.like_count""",
                asdict(c),
            )
        for content_id, breakdown, key, value in result.geo:
            conn.execute(
                """INSERT OR REPLACE INTO geo (platform, content_id, fetched_at, breakdown, key, value)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (platform, content_id, now, breakdown, key, value),
            )
        if result.followers is not None:
            conn.execute(
                "INSERT OR REPLACE INTO account_snapshots VALUES (?, ?, ?)",
                (platform, now, result.followers),
            )


def start_run(conn: sqlite3.Connection, platform: str) -> int:
    with conn:
        cur = conn.execute("INSERT INTO runs (platform, started_at) VALUES (?, ?)", (platform, utcnow()))
    return cur.lastrowid


def finish_run(conn, run_id: int, ok: bool, api_calls: int = 0, posts: int = 0, error: str | None = None):
    # Metering must never crash the pipeline.
    try:
        with conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, ok=?, api_calls=?, posts=?, error=? WHERE id=?",
                (utcnow(), int(ok), api_calls, posts, (error or "")[:500] or None, run_id),
            )
    except sqlite3.Error:
        pass
