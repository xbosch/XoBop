from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


async def exec_shell_command(
    *,
    skill_name: str,
    requires_force: bool,
    policies: dict,
    user_query: str,
    args: dict,
    classify_shell: Callable[[str], Any],
    user_forced: Callable[[str], bool],
    run_shell_async: Callable[[str, str, int, bool], Awaitable[tuple[str, str, int]]],
    shorten_or_artifact: Callable[[str, str], tuple[str, Optional[str]]],
) -> tuple[str, Optional[int], str, Optional[str]]:
    command = args["command"].strip()
    cwd = str(args.get("cwd") or policies.get("default_cwd") or ".")
    raw = bool(args.get("raw", False))
    timeout = int(args.get("timeout_override") or 60)

    # Normalize common shorthand
    command = re.sub(r"\bgit\s+(.+?)\s+push\s+force\b", r"git \1 push --force", command)

    policy = classify_shell(command)
    allowed_commands = [x.strip() for x in policies.get("allowed_commands", "").split(",") if x.strip()]

    # Hard destructive blacklist: never allow these patterns via runtime shell.
    hard_block = [
        r"\brm\s+-rf\s+/$",
        r"\brm\s+-rf\s+/\s",
        r"\bmkfs(\.|\s)",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bpoweroff\b",
    ]
    for pat in hard_block:
        if re.search(pat, command, re.I):
            return f"⛔ Hard-blocked destructive command pattern: {pat}", None, "blocked", None

    raw_allowed = policies.get("allow_raw_shell", "0") == "1"
    if raw and not raw_allowed:
        return "⛔ Raw shell disabled by skill policy.", None, "blocked", None
    if (requires_force or policy.requires_force) and not user_forced(user_query):
        return f"⛔ Blocked ({policy.kind}, raw={raw}). Add 'force' to proceed.\nCommand: {command}", None, "blocked", None

    segments = [s.strip() for s in command.split("&&") if s.strip()]
    if not segments:
        return "⛔ Empty command.", None, "blocked", None

    # Human-in-the-loop gate for write-like operations outside allowed root.
    write_tokens = ("rm ", "mv ", "cp ", "tee ", "sed -i", ">", "mkdir ", "touch ", "chmod ", "chown ")
    write_root = Path(policies.get("write_root") or ".").resolve()
    for seg in segments:
        if not any(tok in seg for tok in write_tokens):
            continue

        targets: list[str] = []
        try:
            argv = shlex.split(seg)
        except Exception:
            argv = []

        if argv:
            # Redirection targets
            for i, tok in enumerate(argv):
                if tok in {">", ">>", "1>", "1>>", "2>", "2>>"} and i + 1 < len(argv):
                    targets.append(argv[i + 1])

            # File-like args for common write commands
            if argv[0] in {"mkdir", "touch", "chmod", "chown", "rm", "mv", "cp", "tee", "sed", "perl"}:
                for a in argv[1:]:
                    if a.startswith("-") or a in {">", ">>", "1>", "1>>", "2>", "2>>"}:
                        continue
                    targets.append(a)

        # Fallback absolute token scan (ignore quoted/script text containing whitespace/newlines)
        if not targets:
            for m in re.findall(r"(/[^\s'\"]+)", seg):
                targets.append(m)

        for t in targets:
            if not t.startswith("/"):
                continue
            if any(ch.isspace() for ch in t):
                continue
            p = Path(t).resolve()
            if not str(p).startswith(str(write_root)) and not user_forced(user_query):
                return f"⛔ Write-like command targets path outside allowed root ({write_root}). Add 'force' to proceed. Path: {p}", None, "blocked", None

    outputs = [f"Kind: {policy.kind}", f"CWD: {cwd}"]
    last_rc: Optional[int] = 0
    for i, seg in enumerate(segments, start=1):
        argv = shlex.split(seg) if seg else []
        if allowed_commands and argv and argv[0] not in allowed_commands:
            return f"⛔ Command '{argv[0]}' blocked by skill allowlist.", None, "blocked", None

        needs_shell = bool(re.search(r"(>|<|\||;|\$\(|`)", seg))
        run_raw = bool(raw or needs_shell)
        if run_raw and not raw_allowed:
            return "⛔ Shell metacharacters detected but raw shell is disabled by policy (set ALLOW_RAW_SHELL=1).", None, "blocked", None

        stdout, stderr, rc = await run_shell_async(seg, cwd, timeout, raw=run_raw)
        outputs.append(f"[step {i}] $ {seg}")
        if stdout.strip():
            outputs.append(f"STDOUT:\n{stdout.strip()}")
        if stderr.strip():
            outputs.append(f"STDERR:\n{stderr.strip()}")
        outputs.append(f"Exit: {rc}")
        last_rc = rc
        if rc != 0:
            break

    result, artifact = shorten_or_artifact(skill_name, "\n".join(outputs))
    status = "ok" if (last_rc == 0) else "error"
    return result, last_rc, status, artifact
