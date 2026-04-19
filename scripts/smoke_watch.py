#!/usr/bin/env python3
"""Interactive smoke verifier for XoBop.

User sends commands manually in Telegram.
This script watches memory.db for assistant replies and validates expected tokens.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "memory.db"
CHAT_ID = 5410368885

TESTS = [
    ("/status", ["Model:", "Mode:"]),
    ("hello", []),
    ("/promptshow", ["Mode:", "Prompt chars:"]),
    ("git status", ["On branch"]),
    ("can you see how many files do I have on my download folder?", ["files in Downloads"]),
    ("/cronlist", []),
]


def db_max_id(db_path: Path, chat_id: int) -> int:
    with sqlite3.connect(str(db_path)) as con:
        r = con.execute("SELECT COALESCE(MAX(id),0) FROM conversations WHERE chat_id=?", (chat_id,)).fetchone()
    return int(r[0] if r else 0)


def wait_reply(db_path: Path, chat_id: int, after_id: int, timeout_s: int = 40) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        with sqlite3.connect(str(db_path)) as con:
            r = con.execute(
                "SELECT id,content FROM conversations WHERE chat_id=? AND role='assistant' AND id>? ORDER BY id DESC LIMIT 1",
                (chat_id, after_id),
            ).fetchone()
        if r:
            return str(r[1] or "")
        time.sleep(0.8)
    raise TimeoutError("Timed out waiting for assistant reply")


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive smoke verifier for XoBop")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to memory.db")
    parser.add_argument("--chat-id", type=int, default=CHAT_ID, help="Chat id to watch")
    args = parser.parse_args()

    db_path = Path(args.db)
    chat_id = int(args.chat_id)

    if not db_path.exists():
        print(f"Missing DB: {db_path}")
        return 2

    print("XoBop Interactive Smoke Watch")
    print("Send each command in Telegram when prompted.\n")

    for i, (cmd, must) in enumerate(TESTS, start=1):
        before = db_max_id(db_path, chat_id)
        if not cmd.strip():
            print(f"FAIL {i}: empty command in TESTS")
            return 1
        print(f"\n[{i}/{len(TESTS)}] Send this in Telegram: {cmd!r}")
        input("After sending it, press Enter here to verify...")
        try:
            reply = wait_reply(db_path, chat_id, before)
        except TimeoutError as e:
            print(f"FAIL {i}: {e}")
            return 1

        missing = [m for m in must if m not in reply]
        if missing:
            print(f"FAIL {i}: missing {missing}\nReply:\n{reply}\n")
            return 1
        print(f"PASS {i}")

    print("\nSMOKE WATCH RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
