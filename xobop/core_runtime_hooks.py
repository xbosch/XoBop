from __future__ import annotations

import json


async def graceful_shutdown_impl(*, app, shutdown_impl, log) -> None:
    await shutdown_impl(app=app, log=log)


async def on_error_impl(*, update, context, on_error_handler, log) -> None:
    await on_error_handler(update=update, context=context, log=log)


def apply_self_mods_impl(*, sm_apply_defaults, get_setting, set_setting, export_self_mods_debug) -> None:
    sm_apply_defaults(get_setting=get_setting, set_setting=set_setting)
    export_self_mods_debug()


def startup_reconcile_impl(*, load_snapshot, apply_self_mods_to_runtime, export_runtime_snapshot_fn, get_last_applied_artifact_id, state_check_dict, repair_state_from_snapshot, set_setting):
    from .runtime_snapshot import startup_reconcile_runtime_state as rsnap_startup_reconcile

    return rsnap_startup_reconcile(
        load_snapshot=load_snapshot,
        apply_self_mods_to_runtime=apply_self_mods_to_runtime,
        export_runtime_snapshot_fn=export_runtime_snapshot_fn,
        get_last_applied_artifact_id=get_last_applied_artifact_id,
        state_check_dict=state_check_dict,
        repair_state_from_snapshot=repair_state_from_snapshot,
        set_setting=set_setting,
        json_dumps=json.dumps,
    )
