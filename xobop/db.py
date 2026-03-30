from __future__ import annotations

import sqlite3
import subprocess
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


def sqlite_online_backup(db_file: str, backup_dir: str, repo_dir: str | None = None) -> str:
    """Create an atomic online backup using sqlite backup API.

    Safe for live DBs in WAL mode.
    Returns backup file path with timestamp + git short hash when available.
    """
    src_path = Path(db_file).resolve()
    dst_dir = Path(backup_dir).resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)

    git_hash = "nogit"
    if repo_dir:
        try:
            out = subprocess.check_output([
                "git", "-C", str(Path(repo_dir).resolve()), "rev-parse", "--short", "HEAD"
            ], stderr=subprocess.DEVNULL, text=True).strip()
            if out:
                git_hash = out
        except Exception:
            pass

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dst_path = dst_dir / f"{src_path.stem}_{ts}_{git_hash}.db"

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
