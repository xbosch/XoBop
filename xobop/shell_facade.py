from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol


@dataclass
class CapabilityResult:
    ok: bool
    output: Any = None
    error: Optional[str] = None
    side_effect: Optional[dict] = None
    metadata: dict = field(default_factory=dict)


class ShellExecutor(Protocol):
    async def run(self, command: str, cwd: str | None = None, timeout: int | None = None, raw: bool = False) -> CapabilityResult: ...


class LiveShellExecutor:
    def __init__(self, legacy_runner: Callable[[str, Optional[str], Optional[int], bool], Awaitable[tuple[str, Optional[int], str, Optional[str]]]], manifest_recorder=None):
        self._legacy_runner = legacy_runner
        self._manifest = manifest_recorder

    async def run(self, command: str, cwd: str | None = None, timeout: int | None = None, raw: bool = False) -> CapabilityResult:
        side = {"type": "shell_run", "command": command, "cwd": cwd, "mode": "live"}
        if self._manifest is not None:
            try:
                self._manifest.record(side)
            except Exception:
                pass
        out = await self._legacy_runner(command, cwd, timeout, raw)
        result, exit_code, status, artifact = out
        return CapabilityResult(
            ok=(status == "ok"),
            output=out,
            error=(result if status != "ok" else None),
            side_effect=side,
            metadata={"status": status, "exit_code": exit_code, "artifact": artifact},
        )


class SimShellExecutor:
    def __init__(self, manifest_recorder=None, command_policy_checker: Optional[Callable[[str], dict]] = None):
        self._manifest = manifest_recorder
        self._policy = command_policy_checker or (lambda _cmd: {"ok": True})

    async def run(self, command: str, cwd: str | None = None, timeout: int | None = None, raw: bool = False) -> CapabilityResult:
        parse_ok = True
        err = ""
        try:
            shlex.split(command)
        except Exception as exc:
            parse_ok = False
            err = str(exc)

        side = {
            "type": "shell_run",
            "command": command,
            "cwd": cwd,
            "mode": "simulated",
            "would_execute": parse_ok,
            "raw": bool(raw),
        }
        if self._manifest is not None:
            try:
                self._manifest.record(side)
            except Exception:
                pass

        if not parse_ok:
            return CapabilityResult(ok=False, error=f"shell parse failed: {err}", side_effect=side)

        pol = self._policy(command) or {}
        if not bool(pol.get("ok", True)):
            return CapabilityResult(ok=False, error=str(pol.get("reason", "blocked")), side_effect=side, metadata={"blocked": True})

        return CapabilityResult(ok=True, output="[simulated shell success]", side_effect=side)


def get_shell_executor(*, mode: str, legacy_runner=None, manifest_recorder=None, command_policy_checker=None) -> ShellExecutor:
    if mode == "simulate":
        return SimShellExecutor(manifest_recorder=manifest_recorder, command_policy_checker=command_policy_checker)
    if legacy_runner is None:
        raise ValueError("legacy_runner is required for live mode")
    return LiveShellExecutor(legacy_runner=legacy_runner, manifest_recorder=manifest_recorder)
