#!/usr/bin/env python3
"""XoBop end-to-end smoke test.

Sends Telegram messages to the bot and verifies bot replies by reading memory_v5.db.
This avoids racing getUpdates against the running bot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "memory_v5.db"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def tg_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="ignore"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram send failed: {body}")


def db_max_id(conn: sqlite3.Connection, chat_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM conversations WHERE chat_id=?", (chat_id,)
    ).fetchone()
    return int(row[0] if row else 0)


def wait_assistant_reply(conn: sqlite3.Connection, chat_id: int, after_id: int, timeout_s: int = 25) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        row = conn.execute(
            """
            SELECT id, content
            FROM conversations
            WHERE chat_id=? AND role='assistant' AND id>?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, after_id),
        ).fetchone()
        if row:
            return str(row[1] or "")
        time.sleep(0.8)
    raise TimeoutError("Timed out waiting for assistant reply")


def step(conn: sqlite3.Connection, token: str, chat_id: int, name: str, text: str, must_contain: list[str] | None = None) -> str:
    before = db_max_id(conn, chat_id)
    tg_send(token, str(chat_id), text)
    reply = wait_assistant_reply(conn, chat_id, before)
    if "LLM error" in reply or "⚠️ Error" in reply:
        raise AssertionError(f"{name} failed with bot error: {reply}")
    if must_contain:
        missing = [m for m in must_contain if m not in reply]
        if missing:
            raise AssertionError(f"{name} missing tokens {missing}. Reply was:\n{reply}")
    print(f"[PASS] {name}")
    return reply


def main() -> int:
    if not ENV_PATH.exists():
        print("Missing .env", file=sys.stderr)
        return 2
    if not DB_PATH.exists():
        print("Missing memory_v5.db", file=sys.stderr)
        return 2

    env = load_env(ENV_PATH)
    token = env.get("TELEGRAM_TOKEN")
    chat_id = env.get("OWNER_ID", "5410368885")
    cron_job = os.getenv("SMOKE_CRON_JOB", "inbox-summary-2030")
    marker = f"smoke-marker-{int(time.time())}"

    if not token:
        print("Missing TELEGRAM_TOKEN in .env", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(DB_PATH))
    try:
        step(conn, token, int(chat_id), "status", "/status", ["Model:", "Mode:"])
        step(conn, token, int(chat_id), "promptshow", "/promptshow", ["Mode:", "Prompt chars:"])
        step(
            conn,
            token,
            int(chat_id),
            "direct git status",
            "git -C /home/xavi/.openclaw/workspace/xobops-test status",
            ["On branch"],
        )
        step(conn, token, int(chat_id), "memadd", f"/memadd {marker}", ["Memory #"])
        mem_reply = step(conn, token, int(chat_id), "memories", "/memories 10")
        if marker not in mem_reply:
            raise AssertionError(f"marker not found in /memories output: {marker}")

        step(conn, token, int(chat_id), "cronlist", "/cronlist", [cron_job])
        step(conn, token, int(chat_id), "cronrun", f"/cronrun {cron_job}", ["Running job"])
        runs = step(conn, token, int(chat_id), "cronruns", f"/cronruns {cron_job}")
        if "No runs for job" in runs:
            raise AssertionError(f"cron job has no runs: {cron_job}")

        print("\nSMOKE TEST RESULT: PASS")
        return 0
    except Exception as exc:
        print(f"\nSMOKE TEST RESULT: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
