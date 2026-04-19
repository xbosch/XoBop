from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


class TempDbManager:
    def __init__(self, live_db_path: str, work_root: str):
        self.live_db_path = Path(live_db_path)
        self.work_root = Path(work_root)

    def fork(self, run_id: str) -> str:
        run_dir = self.work_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        temp_db = run_dir / "memory.db"

        # Safe online snapshot from live DB
        src = sqlite3.connect(str(self.live_db_path))
        dst = sqlite3.connect(str(temp_db))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return str(temp_db)

    def cleanup(self, run_id: str) -> None:
        shutil.rmtree(self.work_root / run_id, ignore_errors=True)
