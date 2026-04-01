from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def export_runtime_snapshot(*, snapshot_file: Path, payload_builder: Callable[..., dict[str, Any]], last_applied_artifact_id: int | None = None, last_promotion_mode: str = "") -> None:
    data = payload_builder(last_applied_artifact_id=last_applied_artifact_id, last_promotion_mode=last_promotion_mode)
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def load_runtime_snapshot(*, snapshot_file: Path) -> dict[str, Any]:
    try:
        if snapshot_file.exists():
            d = json.loads(snapshot_file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def startup_reconcile_runtime_state(
    *,
    load_snapshot: Callable[[], dict[str, Any]],
    apply_self_mods_to_runtime: Callable[[], None],
    export_runtime_snapshot_fn: Callable[..., None],
    get_last_applied_artifact_id: Callable[[], int],
    state_check_dict: Callable[[], dict[str, Any]],
    repair_state_from_snapshot: Callable[[], str],
    set_setting: Callable[[str, str], None],
    json_dumps: Callable[..., str],
) -> None:
    snap = load_snapshot()
    if not snap:
        apply_self_mods_to_runtime()
        export_runtime_snapshot_fn(last_applied_artifact_id=get_last_applied_artifact_id())
        return

    check = state_check_dict()
    if not check["match"]:
        repair_state_from_snapshot()
        set_setting("runtime_state_drift", json_dumps(check.get("mismatched", {}), sort_keys=True))
    else:
        set_setting("runtime_state_drift", "{}")
