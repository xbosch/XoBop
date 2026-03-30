#!/usr/bin/env python3
"""Interactive smoke verifier for XoBop.

User sends commands manually in Telegram.
This script watches memory_v5.db for assistant replies and validates expected tokens.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path('/home/xavi/.openclaw/workspace/xobops-test/memory_v5.db')
CHAT_ID = 5410368885

TESTS = [
    ("/status", ["Model:", "Mode:"]),
    ("/promptshow", ["Mode:", "Prompt chars:"]),
    ("git -C /home/xavi/.openclaw/workspace/xobops-test status", ["On branch"]),
    ("/cronlist", ["inbox-summary-2030"]),
]


def db_max_id() -> int:
    with sqlite3.connect(str(DB_PATH)) as con:
        r = con.execute("SELECT COALESCE(MAX(id),0) FROM conversations WHERE chat_id=?", (CHAT_ID,)).fetchone()
    return int(r[0] if r else 0)


def wait_reply(after_id: int, timeout_s: int = 40) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        with sqlite3.connect(str(DB_PATH)) as con:
            r = con.execute(
                "SELECT id,content FROM conversations WHERE chat_id=? AND role='assistant' AND id>? ORDER BY id DESC LIMIT 1",
                (CHAT_ID, after_id),
            ).fetchone()
        if r:
            return str(r[1] or "")
        time.sleep(0.8)
    raise TimeoutError("Timed out waiting for assistant reply")


def main() -> int:
    if not DB_PATH.exists():
        print(f"Missing DB: {DB_PATH}")
        return 2

    print("XoBop Interactive Smoke Watch")
    print("Send each command in Telegram when prompted.\n")

    for i, (cmd, must) in enumerate(TESTS, start=1):
        before = db_max_id()
        if not cmd.strip():
            print(f"FAIL {i}: empty command in TESTS")
            return 1
        print(f"\n[{i}/{len(TESTS)}] Send this in Telegram: {cmd!r}")
        input("After sending it, press Enter here to verify...")
        try:
            reply = wait_reply(before)
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


if __name__ == '__main__':
    raise SystemExit(main())
