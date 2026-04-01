from __future__ import annotations

from typing import Any, Callable


def build_runtime_state_payload(
    *,
    get_command_aliases: Callable[[], dict[str, str]],
    get_exec_retry_limit: Callable[[], int],
    get_last_applied_artifact_id: Callable[[], int],
    last_applied_artifact_id: int | None = None,
    last_promotion_mode: str = "",
) -> dict[str, Any]:
    payload = {
        "command_aliases": get_command_aliases(),
        "exec_retry_limit": get_exec_retry_limit(),
        "last_applied_artifact_id": int(get_last_applied_artifact_id() if last_applied_artifact_id is None else last_applied_artifact_id),
        "last_promotion_mode": (last_promotion_mode or ""),
    }
    payload["command_aliases"] = {str(k): str(v) for k, v in sorted(payload["command_aliases"].items())}
    return payload


def build_state_check_dict(*, snapshot: dict[str, Any], db_payload: dict[str, Any]) -> dict[str, Any]:
    mismatched: dict[str, dict[str, Any]] = {}
    for key in ["command_aliases", "exec_retry_limit", "last_applied_artifact_id"]:
        if snapshot.get(key) != db_payload.get(key):
            mismatched[key] = {"snapshot": snapshot.get(key), "db": db_payload.get(key)}
    return {
        "snapshot_exists": bool(snapshot),
        "match": len(mismatched) == 0 and bool(snapshot),
        "mismatched": mismatched,
        "snapshot": snapshot,
        "db": db_payload,
    }


def repair_from_snapshot(
    *,
    snapshot: dict[str, Any],
    set_command_aliases: Callable[[dict[str, str]], None],
    set_exec_retry_limit: Callable[[int], None],
    set_last_applied_artifact_id: Callable[[int], None],
) -> str:
    if not snapshot:
        return "⛔ No runtime snapshot found."
    aliases = snapshot.get("command_aliases", {})
    if isinstance(aliases, dict):
        set_command_aliases({str(k): str(v) for k, v in aliases.items()})
    retry = snapshot.get("exec_retry_limit", 0)
    try:
        set_exec_retry_limit(int(retry))
    except Exception:
        set_exec_retry_limit(0)
    try:
        set_last_applied_artifact_id(int(snapshot.get("last_applied_artifact_id", 0) or 0))
    except Exception:
        set_last_applied_artifact_id(0)
    return "✅ Runtime settings repaired from snapshot."
