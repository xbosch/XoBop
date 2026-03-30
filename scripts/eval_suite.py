#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Ensure project root is importable when run from scripts/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xobop.capabilities_shell import exec_shell_command
from xobop.router import classify
from xobop.shell_facade import LiveShellExecutor, SimShellExecutor


@dataclass
class ShellPolicy:
    kind: str
    requires_force: bool


class EvalFailure(Exception):
    pass


class Runner:
    def __init__(self) -> None:
        self.total = 0
        self.failed = 0
        self.shell_calls = 0

    def run(self, name: str, fn: Callable[[], None]) -> None:
        self.total += 1
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            self.failed += 1
            print(f"FAIL {name}: {exc}")

    async def _run_shell_async(self, command: str, cwd: str, timeout: int, raw: bool):
        self.shell_calls += 1
        return f"ran: {command}", "", 0


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        raise EvalFailure(msg)


def test_router_basics() -> None:
    assert_true(classify("/cronlist")["name"] == "cron_list", "cron_list classification mismatch")
    assert_true(classify("how many files in downloads")["name"] == "download_count", "download_count classification mismatch")
    assert_true(classify("did you send the email?")["name"] == "email_status", "email_status classification mismatch")
    assert_true(classify("send email to neri saying hi")["name"] == "email_send", "email_send classification mismatch")
    assert_true(classify("git status")["name"] == "direct_shell", "direct_shell classification mismatch")


def build_policies() -> dict:
    return {
        "allowed_commands": "git,python3,ls,pwd,whoami,cat,cp,mv,mkdir,rm,find,grep,sed,awk,ps,top,du,df,systemctl,journalctl",
        "allow_raw_shell": "0",
        "write_root": str(ROOT),
        "default_cwd": str(ROOT),
    }


async def test_shell_hard_block(r: Runner) -> None:
    before = r.shell_calls
    result, code, status, _ = await exec_shell_command(
        skill_name="run_shell",
        requires_force=False,
        policies=build_policies(),
        user_query="please run",
        args={"command": "rm -rf /", "cwd": str(ROOT)},
        classify_shell=lambda _cmd: ShellPolicy(kind="destructive", requires_force=True),
        user_forced=lambda _q: False,
        run_shell_async=r._run_shell_async,
        shorten_or_artifact=lambda _p, t: (t, None),
    )
    assert_true(status == "blocked", "hard-block should return blocked")
    assert_true("Hard-blocked destructive" in result, "hard-block message missing")
    assert_true(code is None, "hard-block should not return exit code")
    assert_true(r.shell_calls == before, "hard-block should not execute shell")


async def test_shell_force_gate(r: Runner) -> None:
    before = r.shell_calls
    result, _, status, _ = await exec_shell_command(
        skill_name="run_shell",
        requires_force=True,
        policies=build_policies(),
        user_query="run this",
        args={"command": "ls", "cwd": str(ROOT)},
        classify_shell=lambda _cmd: ShellPolicy(kind="read", requires_force=False),
        user_forced=lambda _q: False,
        run_shell_async=r._run_shell_async,
        shorten_or_artifact=lambda _p, t: (t, None),
    )
    assert_true(status == "blocked", "force gate should block")
    assert_true("Add 'force'" in result, "force gate message missing")
    assert_true(r.shell_calls == before, "force-gated command should not execute")


async def test_shell_allowlist_block(r: Runner) -> None:
    before = r.shell_calls
    result, _, status, _ = await exec_shell_command(
        skill_name="run_shell",
        requires_force=False,
        policies=build_policies(),
        user_query="force",
        args={"command": "pytest -q", "cwd": str(ROOT)},
        classify_shell=lambda _cmd: ShellPolicy(kind="read", requires_force=False),
        user_forced=lambda _q: True,
        run_shell_async=r._run_shell_async,
        shorten_or_artifact=lambda _p, t: (t, None),
    )
    assert_true(status == "blocked", "allowlist should block unlisted command")
    assert_true("blocked by skill allowlist" in result, "allowlist block message missing")
    assert_true(r.shell_calls == before, "allowlist-blocked command should not execute")


async def test_shell_write_root_gate(r: Runner) -> None:
    before = r.shell_calls
    result, _, status, _ = await exec_shell_command(
        skill_name="run_shell",
        requires_force=False,
        policies=build_policies(),
        user_query="run",
        args={"command": "cp /etc/hosts ./tmp_hosts_copy", "cwd": str(ROOT)},
        classify_shell=lambda _cmd: ShellPolicy(kind="write", requires_force=False),
        user_forced=lambda _q: False,
        run_shell_async=r._run_shell_async,
        shorten_or_artifact=lambda _p, t: (t, None),
    )
    assert_true(status == "blocked", "write-root gate should block")
    assert_true("outside allowed root" in result, "write-root message missing")
    assert_true(r.shell_calls == before, "write-root blocked command should not execute")


class _Manifest:
    def __init__(self) -> None:
        self.entries = []

    def record(self, effect: dict) -> None:
        self.entries.append(dict(effect))


async def test_facade_live_safe_exec() -> None:
    async def legacy_runner(command, cwd, timeout, raw):
        return f"ok:{command}", 0, "ok", None

    ex = LiveShellExecutor(legacy_runner=legacy_runner, manifest_recorder=_Manifest())
    res = await ex.run("ls", cwd=str(ROOT), timeout=10, raw=False)
    assert_true(res.ok, "live facade should succeed on safe command")


async def test_facade_sim_blocked_command() -> None:
    m = _Manifest()
    ex = SimShellExecutor(manifest_recorder=m, command_policy_checker=lambda _c: {"ok": False, "reason": "blocked"})
    res = await ex.run("rm -rf /", cwd=str(ROOT))
    assert_true(not res.ok, "sim facade should report blocked command")


async def test_facade_sim_parse_invalid() -> None:
    ex = SimShellExecutor(manifest_recorder=_Manifest(), command_policy_checker=lambda _c: {"ok": True})
    res = await ex.run("echo 'unterminated", cwd=str(ROOT))
    assert_true(not res.ok and "parse failed" in (res.error or ""), "invalid parse should fail in sim")


async def test_facade_manifest_recorded() -> None:
    m = _Manifest()
    ex = SimShellExecutor(manifest_recorder=m, command_policy_checker=lambda _c: {"ok": True})
    await ex.run("ls", cwd=str(ROOT))
    assert_true(len(m.entries) == 1 and m.entries[0].get("type") == "shell_run", "manifest should record shell intent")


async def test_facade_sim_never_executes_real() -> None:
    calls = {"n": 0}

    async def legacy_runner(command, cwd, timeout, raw):
        calls["n"] += 1
        return "ok", 0, "ok", None

    live = LiveShellExecutor(legacy_runner=legacy_runner, manifest_recorder=_Manifest())
    sim = SimShellExecutor(manifest_recorder=_Manifest(), command_policy_checker=lambda _c: {"ok": True})
    await sim.run("ls", cwd=str(ROOT))
    assert_true(calls["n"] == 0, "sim path must not invoke live execution")
    await live.run("ls", cwd=str(ROOT))
    assert_true(calls["n"] == 1, "live path should invoke legacy runner")


def main() -> int:
    r = Runner()

    r.run("router.classify.basics", test_router_basics)
    r.run("shell.hard_block", lambda: asyncio.run(test_shell_hard_block(r)))
    r.run("shell.force_gate", lambda: asyncio.run(test_shell_force_gate(r)))
    r.run("shell.allowlist_block", lambda: asyncio.run(test_shell_allowlist_block(r)))
    r.run("shell.write_root_gate", lambda: asyncio.run(test_shell_write_root_gate(r)))
    r.run("facade.live.safe_exec", lambda: asyncio.run(test_facade_live_safe_exec()))
    r.run("facade.sim.blocked", lambda: asyncio.run(test_facade_sim_blocked_command()))
    r.run("facade.sim.parse_invalid", lambda: asyncio.run(test_facade_sim_parse_invalid()))
    r.run("facade.sim.manifest", lambda: asyncio.run(test_facade_manifest_recorded()))
    r.run("facade.sim.no_live_exec", lambda: asyncio.run(test_facade_sim_never_executes_real()))

    print("---")
    print(f"TOTAL: {r.total}")
    print(f"FAILED: {r.failed}")
    print(f"RESULT: {'PASS' if r.failed == 0 else 'FAIL'}")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
