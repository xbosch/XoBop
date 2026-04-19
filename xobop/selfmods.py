from __future__ import annotations

import json
from pathlib import Path
from typing import Callable


def export_self_mods_debug(
    *,
    self_mods_file: Path,
    get_command_aliases: Callable[[], dict[str, str]],
    get_exec_retry_limit: Callable[[], int],
    get_last_applied_artifact_id: Callable[[], int],
) -> None:
    try:
        payload = {
            "command_aliases": get_command_aliases(),
            "exec_retry_limit": get_exec_retry_limit(),
            "last_applied_artifact_id": get_last_applied_artifact_id(),
        }
        self_mods_file.parent.mkdir(parents=True, exist_ok=True)
        self_mods_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def apply_self_mods_defaults(*, get_setting: Callable[[str, str | None], str | None], set_setting: Callable[[str, str], None]) -> None:
    if get_setting("command_aliases_json") is None:
        set_setting("command_aliases_json", "{}")
    if get_setting("exec_retry_limit") is None:
        set_setting("exec_retry_limit", "0")
    if get_setting("last_applied_artifact_id") is None:
        set_setting("last_applied_artifact_id", "0")
