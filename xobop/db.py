from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


@contextmanager
def db_session(db_file: str) -> Iterator[sqlite3.Connection]:
    """Open SQLite session with runtime defaults and commit/rollback handling."""
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sqlite_online_backup(db_file: str, backup_dir: str) -> str:
    """Create an atomic online backup using sqlite backup API.

    Safe for live DBs in WAL mode.
    Returns backup file path.
    """
    src_path = Path(db_file).resolve()
    dst_dir = Path(backup_dir).resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y-%m-%d")
    dst_path = dst_dir / f"{src_path.stem}_{ts}.db"

    src = sqlite3.connect(str(src_path), check_same_thread=False)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    return str(dst_path)
