from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from ..shell_facade import SimShellExecutor
from .manifest import IntentManifest


@dataclass
class RuntimeContext:
    mode: str
    db_path: str
    sandbox_root: str | None = None
    trace_id: str | None = None
    proposal_id: int | None = None
    record_manifest: bool = False


class ReplayController:
    def __init__(self, command_policy_checker: Callable[[str], dict], diff_builder: Callable[..., dict]):
        self.command_policy_checker = command_policy_checker
        self.diff_builder = diff_builder

    def run(
        self,
        *,
        proposal_id: int,
        artifact: dict,
        replay_cases: list[dict],
        golden_cases: list[dict],
        temp_db_path: str,
        sandbox_root: str,
        apply_artifact_to_temp_state: Callable[[str, dict], None],
    ) -> dict:
        run_id = f"sim_{proposal_id}"
        os.makedirs(sandbox_root, exist_ok=True)
        manifest = IntentManifest(trace_id=run_id)
        ctx = RuntimeContext(mode="simulate", db_path=temp_db_path, sandbox_root=sandbox_root, trace_id=run_id, proposal_id=proposal_id, record_manifest=True)
        _ = ctx

        sim = SimShellExecutor(manifest_recorder=manifest, command_policy_checker=self.command_policy_checker)
        apply_artifact_to_temp_state(temp_db_path, artifact)

        replay_results = []
        for c in replay_cases:
            cmd = c["command"]
            pol = self.command_policy_checker(cmd) or {"ok": True}
            policy_ok = bool(pol.get("ok", True))
            res = asyncio.run(sim.run(cmd, cwd=sandbox_root or "."))
            replay_results.append(
                {
                    "case_id": c["case_id"],
                    "case_class": c.get("case_class", "targeted"),
                    "command": cmd,
                    "expect_ok": bool(c.get("expect_ok", True)),
                    "actual_ok": bool(res.ok),
                    "outcome": "success" if res.ok else "failure",
                    "policy_ok": policy_ok,
                    "policy_reason": pol.get("reason", ""),
                }
            )

        golden_results = []
        for c in golden_cases:
            res = asyncio.run(sim.run(c["command"], cwd="."))
            passed = bool(res.ok) == bool(c.get("expect_ok", True))
            golden_results.append(
                {
                    "case_id": c["case_id"],
                    "command": c["command"],
                    "pass": passed,
                    "expected_ok": bool(c.get("expect_ok", True)),
                    "actual_ok": bool(res.ok),
                }
            )

        report = self.diff_builder(
            proposal_id=proposal_id,
            artifact=artifact,
            replay_results=replay_results,
            golden_results=golden_results,
            manifest=manifest,
        )
        report["temp_db_path"] = temp_db_path
        report["sandbox_root"] = sandbox_root
        report["manifest_json"] = json.dumps(manifest.summary())
        return report


def apply_artifact_to_temp_settings(temp_db_path: str, artifact: dict) -> None:
    con = sqlite3.connect(temp_db_path)
    try:
        cur = con.cursor()
        ajson = artifact.get("artifact_json", {})
        if isinstance(ajson, str):
            try:
                ajson = json.loads(ajson)
            except Exception:
                ajson = {}

        if artifact.get("artifact_type") == "routing_rule" and artifact.get("target") == "unknown_command_handler":
            aliases = (ajson or {}).get("add_alias", {})
            cur.execute(
                "INSERT OR REPLACE INTO settings (key,value,timestamp) VALUES ('command_aliases_json', ?, CURRENT_TIMESTAMP)",
                (json.dumps(aliases, sort_keys=True),),
            )
        elif artifact.get("artifact_type") == "config_change" and artifact.get("target") == "runtime_retry_policy":
            key = (ajson or {}).get("key", "exec_retry_limit")
            val = str((ajson or {}).get("value", "0"))
            cur.execute(
                "INSERT OR REPLACE INTO settings (key,value,timestamp) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, val),
            )
        elif artifact.get("artifact_type") == "skill_policy_change" and artifact.get("target") == "runtime_skill_config":
            skill = str((ajson or {}).get("skill", "")).strip()
            policy = str((ajson or {}).get("policy", "")).strip().lower()
            value = str((ajson or {}).get("value", "")).strip()
            if skill and policy and value:
                cur.execute(
                    "INSERT OR REPLACE INTO skill_policies (skill_name,policy_key,policy_value) VALUES (?,?,?)",
                    (skill, policy, value),
                )
        con.commit()
    finally:
        con.close()
