from __future__ import annotations

import hashlib
import json
from typing import Any, Callable


def artifact_contract(artifact_type: str, target: str) -> dict[str, Any]:
    at = (artifact_type or "").strip()
    tg = (target or "").strip()
    if at == "routing_rule" and tg == "unknown_command_handler":
        base = {
            "family": "alias",
            "apply_points": ["command_pre_dispatch", "unknown_command_suggestion"],
            "invariants": ["alias_applies_before_dispatch"],
        }
    elif at == "config_change" and tg == "runtime_retry_policy":
        base = {
            "family": "retry",
            "apply_points": ["runtime_settings"],
            "invariants": ["retry_limit_in_range"],
        }
    elif at == "state_lineage" and tg == "runtime_snapshot":
        base = {
            "family": "lineage",
            "apply_points": ["runtime_settings", "snapshot_export", "startup_reconcile", "state_check"],
            "invariants": ["snapshot_lineage_nonzero"],
        }
    elif at == "prompt_change" and tg == "runtime_prompt":
        base = {
            "family": "prompt",
            "apply_points": ["runtime_prompt_store", "prompt_replay_gate"],
            "invariants": ["prompt_nonempty", "prompt_golden_replay_pass"],
        }
    elif at == "skill_policy_change" and tg == "runtime_skill_config":
        base = {
            "family": "skill_policy",
            "apply_points": ["skill_policies", "runtime_settings"],
            "invariants": ["skill_policy_keys_allowed", "skill_policy_values_valid"],
        }
    else:
        return {"family": "unknown", "apply_points": [], "invariants": [], "contract_version": "v0", "contract_hash": ""}

    canonical = json.dumps(
        {
            "family": base["family"],
            "apply_points": base["apply_points"],
            "invariants": base["invariants"],
            "contract_version": "v1",
        },
        sort_keys=True,
    )
    base["contract_version"] = "v1"
    base["contract_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return base


def artifact_has_expected_contract(artifact_type: str, target: str, artifact_json: dict[str, Any]) -> tuple[bool, str]:
    expected = artifact_contract(artifact_type, target)
    if expected.get("family") == "unknown":
        return False, f"unsupported artifact scope: {artifact_type}:{target}"
    allowed_contract_keys = {"apply_points", "invariants", "contract_version", "contract_hash"}
    extra_contract_keys = [k for k in artifact_json.keys() if str(k).startswith("contract_") and str(k) not in allowed_contract_keys]
    if extra_contract_keys:
        return False, f"unknown contract keys: {','.join(sorted(extra_contract_keys))}"
    if artifact_json.get("apply_points") != expected.get("apply_points"):
        return False, "apply_points mismatch"
    if artifact_json.get("invariants") != expected.get("invariants"):
        return False, "invariants mismatch"
    if str(artifact_json.get("contract_version", "")) != str(expected.get("contract_version", "")):
        return False, "contract_version mismatch"
    if str(artifact_json.get("contract_hash", "")) != str(expected.get("contract_hash", "")):
        return False, "contract_hash mismatch"
    return True, "ok"


def apply_artifact_to_runtime_settings(
    artifact_type: str,
    target: str,
    artifact_json: dict[str, Any],
    *,
    get_command_aliases: Callable[[], dict[str, str]],
    set_command_aliases: Callable[[dict[str, str]], None],
    set_exec_retry_limit: Callable[[int], None],
    set_last_applied_artifact_id: Callable[[int], None],
    set_runtime_prompt: Callable[[str, str], None] | None = None,
    set_skill_policy: Callable[[str, str, str], None] | None = None,
) -> str:
    atype = (artifact_type or "").strip()
    t = (target or "").strip()

    if atype == "routing_rule" and t == "unknown_command_handler":
        aliases = dict(get_command_aliases())
        for k, v in dict((artifact_json or {}).get("add_alias", {})).items():
            aliases[str(k).strip().lower()] = str(v).strip().lower()
        set_command_aliases(aliases)
        return f"aliases={len(aliases)}"

    if atype == "config_change" and t == "runtime_retry_policy":
        key = str((artifact_json or {}).get("key", "")).strip()
        val = str((artifact_json or {}).get("value", "")).strip()
        if key != "exec_retry_limit" or (not val.isdigit()):
            raise ValueError("invalid retry policy payload")
        set_exec_retry_limit(int(val))
        return f"{key}={val}"

    if atype == "state_lineage" and t == "runtime_snapshot":
        aid = int((artifact_json or {}).get("artifact_id", 0) or 0)
        set_last_applied_artifact_id(aid)
        return f"last_applied_artifact_id={aid}"

    if atype == "prompt_change" and t == "runtime_prompt":
        mode = str((artifact_json or {}).get("mode", "")).strip().lower()
        value = str((artifact_json or {}).get("value", ""))
        if mode not in {"strict", "normal", "overseer"} or not value.strip():
            raise ValueError("invalid prompt payload")
        if set_runtime_prompt is None:
            raise ValueError("runtime prompt setter unavailable")
        set_runtime_prompt(mode, value)
        return f"prompt[{mode}]={len(value)} chars"

    if atype == "skill_policy_change" and t == "runtime_skill_config":
        if set_skill_policy is None:
            raise ValueError("skill policy setter unavailable")
        skill = str((artifact_json or {}).get("skill", "")).strip()
        policy = str((artifact_json or {}).get("policy", "")).strip().lower()
        value = str((artifact_json or {}).get("value", "")).strip()
        allowed = {"max_calls_per_hour", "allow_domains", "write_root", "default_cwd", "allow_raw_shell"}
        if not skill or policy not in allowed or not value:
            raise ValueError("invalid skill policy payload")
        set_skill_policy(skill, policy, value)
        return f"skill_policy[{skill}:{policy}]={value}"

    raise ValueError(f"unsupported artifact scope: {atype}:{t}")
