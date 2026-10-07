"""SQLite storage. One file, no server to run."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kupuna (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    phone           TEXT NOT NULL,
    call_time       TEXT NOT NULL,          -- local HH:MM
    language        TEXT NOT NULL DEFAULT 'English',
    contact1_name   TEXT NOT NULL,
    contact1_phone  TEXT NOT NULL,
    contact2_name   TEXT NOT NULL,
    contact2_phone  TEXT NOT NULL,
    consent_note    TEXT NOT NULL,          -- who agreed, how, when
    consent_at      TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    removed_at      TEXT                    -- set when taken off the list; history is kept
);

CREATE TABLE IF NOT EXISTS checkins (
    id              INTEGER PRIMARY KEY,
    kupuna_id       INTEGER NOT NULL REFERENCES kupuna(id),
    day             TEXT NOT NULL,          -- local YYYY-MM-DD
    status          TEXT NOT NULL,          -- see engine.STATUSES
    attempts        INTEGER NOT NULL DEFAULT 0,
    call_sid        TEXT,
    last_call_at    TEXT,
    next_at         TEXT,                   -- UTC time the next step is due
    alerted_at      TEXT,
    backup_at       TEXT,
    resolved_by     TEXT,
    resolved_at     TEXT,
    UNIQUE (kupuna_id, day)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    checkin_id  INTEGER REFERENCES checkins(id),
    kupuna_id   INTEGER REFERENCES kupuna(id),
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signups (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',   -- new, approved, declined
    plan            TEXT NOT NULL,
    family_name     TEXT NOT NULL,
    family_phone    TEXT NOT NULL,
    family_email    TEXT NOT NULL,
    relationship    TEXT NOT NULL,
    kupuna_name     TEXT NOT NULL,
    kupuna_phone    TEXT NOT NULL,
    call_time       TEXT NOT NULL,
    language        TEXT NOT NULL,
    backup_name     TEXT NOT NULL,
    backup_phone    TEXT NOT NULL,
    notes           TEXT NOT NULL DEFAULT '',
    kupuna_id       INTEGER REFERENCES kupuna(id),
    decided_at      TEXT
);

CREATE TABLE IF NOT EXISTS summaries (
    kupuna_id   INTEGER NOT NULL REFERENCES kupuna(id),
    week_ending TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (kupuna_id, week_ending)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL") if path != ":memory:" else None
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to date without losing anything."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(kupuna)")}
    if "removed_at" not in cols:
        conn.execute("ALTER TABLE kupuna ADD COLUMN removed_at TEXT")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None
