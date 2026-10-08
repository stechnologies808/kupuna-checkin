"""Backups of the one database file.

- daily_copy(): once a day the scheduler saves a copy next to the database (in backups/),
  keeping the last KEEP_DAYS copies, so a mistake can be undone.
- snapshot_bytes(): a full copy to download from the admin page and keep somewhere else.
- people_csv(): the list of kūpuna and their contacts as a spreadsheet anyone can open.
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

KEEP_DAYS = 14
PREFIX = "checkin-"


def backup_dir(db_path: str) -> Path | None:
    if not db_path or db_path == ":memory:":
        return None
    return Path(db_path).resolve().parent / "backups"


def _copy_to(conn: sqlite3.Connection, dest: Path) -> None:
    """SQLite's own backup: a consistent copy even while the app is writing."""
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()


def daily_copy(conn: sqlite3.Connection, db_path: str, today: date, keep: int = KEEP_DAYS) -> Path | None:
    """Make today's copy if it isn't there yet and drop copies older than `keep` days.
    Returns the new copy's path, or None when nothing was needed."""
    folder = backup_dir(db_path)
    if folder is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{PREFIX}{today.isoformat()}.db"
    made = None
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        _copy_to(conn, tmp)
        os.replace(tmp, dest)  # never leave a half-written copy with a real name
        made = dest
    for old in sorted(folder.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return made


def latest_copy(db_path: str) -> str | None:
    folder = backup_dir(db_path)
    if folder is None or not folder.exists():
        return None
    copies = sorted(folder.glob(f"{PREFIX}*.db"))
    return copies[-1].stem[len(PREFIX):] if copies else None


def snapshot_bytes(conn: sqlite3.Connection) -> bytes:
    fd, name = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _copy_to(conn, Path(name))
        return Path(name).read_bytes()
    finally:
        os.unlink(name)


def _cell(v) -> str:
    s = "" if v is None else str(v)
    # Stop a spreadsheet from treating a name like "=SUM(...)" as a formula.
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


COLUMNS = [("name", "Name"), ("phone", "Phone"), ("call_time", "Call time"), ("language", "Language"),
           ("contact1_name", "Family contact"), ("contact1_phone", "Family phone"),
           ("contact2_name", "Backup contact"), ("contact2_phone", "Backup phone"),
           ("status", "Status"), ("consent_note", "Consent"), ("created_at", "Added"),
           ("removed_at", "Removed")]


def people_csv(conn: sqlite3.Connection) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([label for _, label in COLUMNS])
    for r in conn.execute("SELECT * FROM kupuna ORDER BY removed_at IS NOT NULL, name"):
        row = dict(r)
        row["status"] = "Removed" if r["removed_at"] else ("Active" if r["active"] else "Paused")
        w.writerow([_cell(row.get(k)) for k, _ in COLUMNS])
    return out.getvalue()
