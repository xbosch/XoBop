from __future__ import annotations

import ctypes
import json
import os
from datetime import datetime
from typing import Callable


def set_process_name(name: str) -> None:
    try:
        libc = ctypes.CDLL(None)
        PR_SET_NAME = 15
        b = name.encode("utf-8")[:15]
        libc.prctl(PR_SET_NAME, ctypes.c_char_p(b), 0, 0, 0)
    except Exception:
        pass


def acquire_runtime_lock(*, get_setting: Callable[[str, str], str], set_setting: Callable[[str, str], None], token: str) -> tuple[bool, str]:
    now = int(datetime.now().timestamp())
    proc_name = f"xobop-{datetime.now().strftime('%m%d-%H%M%S')}"
    set_process_name(proc_name)
    me = {
        "pid": os.getpid(),
        "started_ts": now,
        "bot_id": (token.split(":", 1)[0] if ":" in token else "unknown"),
        "proc_name": proc_name,
    }
    raw = get_setting("runtime_lock_json", "{}") or "{}"
    try:
        cur = json.loads(raw)
    except Exception:
        cur = {}
    other_pid = int(cur.get("pid", 0) or 0)
    if other_pid and other_pid != me["pid"]:
        alive = True
        try:
            os.kill(other_pid, 0)
        except Exception:
            alive = False
        if alive:
            looks_like_xobop = False
            try:
                with open(f"/proc/{other_pid}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\0", b" ").decode("utf-8", errors="ignore").lower()
                looks_like_xobop = ("xobop" in cmd) or ("python -" in cmd)
            except Exception:
                looks_like_xobop = False
            if looks_like_xobop:
                return False, f"locked by pid={other_pid}"
    set_setting("runtime_lock_json", json.dumps(me, sort_keys=True))
    return True, f"pid={me['pid']}"


def release_runtime_lock(*, get_setting: Callable[[str, str], str], set_setting: Callable[[str, str], None]) -> None:
    raw = get_setting("runtime_lock_json", "{}") or "{}"
    try:
        cur = json.loads(raw)
    except Exception:
        cur = {}
    if int(cur.get("pid", 0) or 0) == os.getpid():
        set_setting("runtime_lock_json", "{}")
