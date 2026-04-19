from xobop.core_runtime_hooks import startup_reconcile_impl


def test_startup_reconcile_threads_json_dumps_param():
    calls = {"set": None, "exported": False}

    def load_snapshot():
        return {"ok": True}

    def apply_self_mods_to_runtime():
        raise AssertionError("should not be called when snapshot exists")

    def export_runtime_snapshot_fn(**kwargs):
        calls["exported"] = True

    def get_last_applied_artifact_id():
        return 0

    def state_check_dict():
        return {"match": False, "mismatched": {"k": "v"}}

    def repair_state_from_snapshot():
        return "ok"

    def set_setting(k, v):
        if k == "runtime_state_drift":
            calls["set"] = v

    startup_reconcile_impl(
        load_snapshot=load_snapshot,
        apply_self_mods_to_runtime=apply_self_mods_to_runtime,
        export_runtime_snapshot_fn=export_runtime_snapshot_fn,
        get_last_applied_artifact_id=get_last_applied_artifact_id,
        state_check_dict=state_check_dict,
        repair_state_from_snapshot=repair_state_from_snapshot,
        set_setting=set_setting,
    )

    assert calls["set"] == '{"k": "v"}'
