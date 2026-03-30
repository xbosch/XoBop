from __future__ import annotations

import sqlite3
from contextlib import contextmanager
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
