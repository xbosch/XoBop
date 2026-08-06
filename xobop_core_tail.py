import xobop_core_base as _base

# Explicit compatibility export (including private symbols used by tests/bridges)
for _k, _v in vars(_base).items():
    if _k == "__builtins__":
        continue
    globals()[_k] = _v

from telegram import Update
from telegram.ext import Application, ContextTypes

from xobop.core_runtime_hooks import (
    apply_self_mods_impl,
    graceful_shutdown_impl,
    on_error_impl,
    startup_reconcile_impl,
)
from xobop.core_runtime_wiring import run_main
from xobop.router import classify as router_classify

_app = None
_state_task = None
_artifact_task = None
_notify_task = None
_memory_promo_task = None
from xobop.core_runtime_loops import artifact_watch_loop_impl, notification_flush_loop_impl
from xobop.core_agent_runtime import run_agent_impl
from xobop.core_health_runtime import health_report_impl, system_pulse_impl
from xobop.core_telegram_io import owner_only_impl, reply_impl
from xobop.core_diagnostics_runtime import run_checkup_sync_impl, generate_system_map_sync_impl
from xobop.core_commands_runtime import cmd_plan_impl, cmd_go_impl, cmd_checkup_impl
from xobop.core_dispatch_runtime import build_unknown_dispatch, build_media_processor

async def _artifact_watch_loop() -> None:
    await artifact_watch_loop_impl(
        artifact_dir=ARTIFACT_DIR,
        app_getter=lambda: _app,
        get_setting=_get_setting,
        quicklook_summary=_quicklook_summary,
        notify_owner=_notify_owner,
        overseer_enabled=_overseer_enabled,
        multimodal_artifact_summary=_multimodal_artifact_summary,
        add_memory=_add_memory,
        log=log,
        asyncio_mod=asyncio,
    )


async def _notification_flush_loop() -> None:
    await notification_flush_loop_impl(
        in_quiet_hours=_in_quiet_hours,
        app_getter=lambda: _app,
        get_db=get_db,
        log=log,
        asyncio_mod=asyncio,
    )


# =============================================================================
# Skill execution
# =============================================================================


def _record_op_event(
    event_type: str,
    *,
    chat_id: int = 0,
    route: str = "",
    command: str = "",
    status: str = "ok",
    latency_ms: Optional[int] = None,
    retries: int = 0,
    metadata: Optional[dict] = None,
) -> None:
    return ol_record_op_event(
        get_db=get_db,
        event_type=event_type,
        chat_id=chat_id,
        route=route,
        command=command,
        status=status,
        latency_ms=latency_ms,
        retries=retries,
        metadata=metadata,
    )


def _record_run(
    chat_id: int,
    user_query: str,
    mode: str,
    tool_name: Optional[str],
    tool_args: Optional[dict],
    result: str,
    exit_code: Optional[int],
    duration_ms: int,
    status: str,
    artifact_path: Optional[str] = None,
) -> None:
    ol_record_run(
        get_db=get_db,
        redact=_redact,
        text_limit=TEXT_LIMIT,
        turn_id=_AGENT_TURN_ID.get("") or "",
        chat_id=chat_id,
        user_query=user_query,
        mode=mode,
        tool_name=tool_name,
        tool_args=tool_args,
        result=result,
        exit_code=exit_code,
        duration_ms=duration_ms,
        status=status,
        artifact_path=artifact_path,
    )
    _record_op_event(
        "tool_run",
        chat_id=chat_id,
        route=(tool_name or ""),
        command=((tool_args or {}).get("command", "") if isinstance(tool_args, dict) else ""),
        status=status,
        latency_ms=duration_ms,
        metadata={"mode": mode, "exit_code": exit_code},
    )


async def _exec_file_read(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    return await sr_exec_file_read(
        skill=skill,
        policies=policies,
        args=args,
        read_root=READ_ROOT,
        max_read_bytes=MAX_READ_BYTES,
        is_under_root=_is_under_root,
        to_thread=_to_thread,
        read_text_limited=_read_text_limited,
        shorten_or_artifact=_shorten_or_artifact,
    )


async def _exec_http_get(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    return await sr_exec_http_get(
        skill=skill,
        policies=policies,
        args=args,
        domain_allowed=_domain_allowed,
        max_browse_bytes=MAX_BROWSE_BYTES,
        to_thread=_to_thread,
        fetch_text_limited=_fetch_text_limited,
        shorten_or_artifact=_shorten_or_artifact,
    )


async def _exec_file_write(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    return await sr_exec_file_write(
        skill=skill,
        policies=policies,
        args=args,
        allowed_write_root=ALLOWED_WRITE_ROOT,
        is_under_root=_is_under_root,
        blocked_suffix=_blocked_suffix,
        get_policy=_get_policy,
        default_max_write_size=DEFAULT_GLOBAL_POLICIES["max_write_size"],
        to_thread=_to_thread,
    )


async def _exec_file_append(skill: SkillDef, policies: dict, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    return await sr_exec_file_append(
        skill=skill,
        policies=policies,
        args=args,
        allowed_write_root=ALLOWED_WRITE_ROOT,
        is_under_root=_is_under_root,
        blocked_suffix=_blocked_suffix,
        get_policy=_get_policy,
        default_max_write_size=DEFAULT_GLOBAL_POLICIES["max_write_size"],
        to_thread=_to_thread,
    )


async def _exec_shell_command(skill: SkillDef, policies: dict, user_query: str, args: dict) -> tuple[str, Optional[int], str, Optional[str]]:
    return await sr_exec_shell_command(
        skill=skill,
        policies=policies,
        user_query=user_query,
        args=args,
        use_capability_facade_shell=USE_CAPABILITY_FACADE_SHELL,
        get_shell_executor=get_shell_executor,
        exec_shell_command_capability=exec_shell_command,
        classify_shell=_classify_shell,
        user_forced=_user_forced,
        run_shell_async=_run_shell_async,
        shorten_or_artifact=_shorten_or_artifact,
    )


async def _call_skill(chat_id: int, user_query: str, mode: str, skill_name: str, args: dict) -> str:
    handlers = {
        "file_read": lambda **kw: _exec_file_read(kw["skill"], kw["policies"], kw["args"]),
        "http_get": lambda **kw: _exec_http_get(kw["skill"], kw["policies"], kw["args"]),
        "file_write": lambda **kw: _exec_file_write(kw["skill"], kw["policies"], kw["args"]),
        "file_append": lambda **kw: _exec_file_append(kw["skill"], kw["policies"], kw["args"]),
        "shell_command": lambda **kw: _exec_shell_command(kw["skill"], kw["policies"], kw["user_query"], kw["args"]),
    }
    return await sr_call_skill_impl(
        chat_id=chat_id,
        user_query=user_query,
        mode=mode,
        skill_name=skill_name,
        args=args,
        panic_locked=_panic_locked,
        record_op_event=_record_op_event,
        get_policy=_get_policy,
        default_global_policies=DEFAULT_GLOBAL_POLICIES,
        enforce_rate_limit=_enforce_rate_limit,
        get_skill=_get_skill,
        load_skill_policies=_load_skill_policies,
        allowed_write_root=ALLOWED_WRITE_ROOT,
        project_root=PROJECT_ROOT,
        allow_raw_shell=ALLOW_RAW_SHELL,
        user_forced=_user_forced,
        handlers=handlers,
        log=log,
        to_thread=_to_thread,
        record_run=_record_run,
        update_skill_usage=_update_skill_usage,
        audit_skill=_audit_skill,
        fire_and_forget=_fire_and_forget,
    )


# =============================================================================
# Agent loop
# =============================================================================

def _temperature_for_mode(mode: str) -> float:
    return mu_temperature_for_mode(mode)


async def _analyze_attachment(chat_id: int, path: str, user_hint: str = "") -> str:
    return await ar_analyze_attachment(chat_id=chat_id, path=path, user_hint=user_hint, state=STATE, client=client, model=MODEL)


async def _run_agent(chat_id: int, query: str, force: bool = False) -> str:
    def _set_last_latency(ms: float) -> None:
        global _last_openai_latency_ms
        _last_openai_latency_ms = ms

    return await run_agent_impl(
        chat_id=chat_id,
        query=query,
        force=force,
        get_mode=_get_mode,
        agent_turn_id=_AGENT_TURN_ID,
        get_history=_get_history,
        build_memory_block=_build_memory_block,
        build_tools_from_db=_build_tools_from_db,
        user_forced=_user_forced,
        get_runtime_prompt=_get_runtime_prompt,
        get_allowed_write_root=lambda: str(ALLOWED_WRITE_ROOT),
        save_message=_save_message,
        maybe_promote_memory=_maybe_promote_memory,
        call_skill=_call_skill,
        redact=_redact,
        normalize_machine_kv_output=normalize_machine_kv_output,
        fire_and_forget=_fire_and_forget,
        set_last_latency_ref=_set_last_latency,
        temperature_for_mode=_temperature_for_mode,
        client=client,
        model=MODEL,
        history_load_limit=HISTORY_LOAD_LIMIT,
        max_memory_results=MAX_MEMORY_RESULTS,
        max_tool_rounds=MAX_TOOL_ROUNDS,
        history_max=MAX_HISTORY,
        history_trim_to=TRIM_TO,
        log=log,
    )


# =============================================================================
# Status and metrics
# =============================================================================


def _system_pulse() -> dict[str, str]:
    return system_pulse_impl()


def _health_report() -> str:
    return health_report_impl(
        get_db=get_db,
        db_file=DB_FILE,
        system_pulse=_system_pulse,
        last_openai_latency_ms=_last_openai_latency_ms,
    )


def _diagnose_report(days: int = 7) -> str:
    return ig_diagnose_report(days=days, get_db=get_db)


def _llm_proposal_complete(prompt: str) -> str:
    try:
        resp = sync_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=500,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


async def _generate_improvement_proposal(days: int = 7, conditioned_rows=None) -> tuple[int, str]:
    d = max(1, min(days, 30))
    if conditioned_rows is None:
        diagnosis_text = _diagnose_report(d)
        # Derive failure_class from diagnosis text so the learned query vector
        # for that class is used instead of the raw diagnosis embedding.
        _fc = "general"
        dt = diagnosis_text.lower()
        if "unknown command" in dt or "discoverability" in dt:
            _fc = "unknown_command"
        elif "shell route" in dt or "run_shell" in dt:
            _fc = "route_failure"
        elif "latency" in dt or "p95 over" in dt:
            _fc = "latency"
        try:
            conditioned_rows = await ipo_fetch_conditioned_evolution_async(
                diagnosis_text=diagnosis_text,
                get_db=get_db,
                embed_fn=_embed_text,
                limit=5,
                failure_class=_fc,
            )
        except Exception:
            conditioned_rows = None
    return await _to_thread(
        lambda: ig_generate_improvement_proposal(
            days=d,
            get_db=get_db,
            diagnose_report_fn=_diagnose_report,
            llm_proposal_fn=_llm_proposal_complete,
            allowed_families_fn=lambda: sorted(_allowed_artifact_families()),
            conditioned_evolution_rows=conditioned_rows,
            priorities_text=_build_improve_priorities_block(),
        )
    )
def _build_improve_priorities_block() -> str:
    return ig_build_improve_priorities_block(settings.improve_priorities_file)
def _latest_artifact_for_proposal(proposal_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    return row


def _compile_proposal_to_change(proposal_id: int) -> str:
    try:
        _snap = ig_diagnose_snapshot(days=7, get_db=get_db)
    except Exception:
        _snap = None
    return ig_compile_proposal_to_change(
        proposal_id=proposal_id,
        get_db=get_db,
        get_command_aliases=_get_command_aliases,
        get_exec_retry_limit=_get_exec_retry_limit,
        get_last_applied_artifact_id=_get_last_applied_artifact_id,
        get_mode=_get_mode,
        get_runtime_prompt=_get_runtime_prompt,
        artifact_contract=_artifact_contract,
        allowed_artifact_families=_allowed_artifact_families,
        diagnosis_snapshot=_snap,
    )
def _sim_shell_policy_check(command: str) -> dict:
    # Single source of truth: use shared shell classifier policy only.
    pol = _classify_shell(command)
    if pol.kind == "destructive":
        return {"ok": False, "reason": "destructive"}
    return {"ok": True}


def _build_shell_replay_diff(*, proposal_id: int, artifact: dict, replay_results: list[dict], golden_results: list[dict], manifest) -> dict:
    targeted = [r for r in replay_results if r.get("case_class") == "targeted"]
    targeted_improved = sum(1 for r in targeted if bool(r.get("actual_ok")) == bool(r.get("expect_ok", True)))
    targeted_regressed = sum(1 for r in targeted if bool(r.get("actual_ok")) != bool(r.get("expect_ok", True)))
    golden_failed = sum(1 for r in golden_results if not r.get("pass"))
    policy_violations = sum(1 for r in replay_results if not bool(r.get("policy_ok", True)))
    msum = manifest.summary()
    summary = {
        "targeted_cases": len(targeted),
        "golden_cases": len(golden_results),
        "targeted_improved": targeted_improved,
        "targeted_regressed": targeted_regressed,
        "golden_failed": golden_failed,
        "policy_violations": policy_violations,
        "side_effects_intercepted": int(msum.get("side_effects_intercepted", 0)),
        "live_side_effects": int(msum.get("live_side_effects", 0)),
    }
    status = "pass"
    if summary["golden_failed"] > 0 or summary["policy_violations"] > 0 or summary["live_side_effects"] != 0:
        status = "fail"
    elif summary["targeted_improved"] <= summary["targeted_regressed"]:
        status = "fail"
    return {
        "proposal_id": proposal_id,
        "artifact_id": int(artifact.get("id") or 0),
        "artifact_type": artifact.get("artifact_type"),
        "target": artifact.get("target"),
        "mode": "simulate",
        "status": status,
        "summary": summary,
        "replay_results": replay_results,
        "golden_results": golden_results,
        "manifest_summary": msum,
    }


def _simulate_proposal_sync(proposal_id: int) -> str:
    import subprocess

    code_path = Path(__file__).resolve()
    repo = code_path.parent
    py = (repo / ".venv" / "bin" / "python")
    if not py.exists():
        py = Path(sys.executable)

    checks: list[tuple[str, bool, str]] = []

    art = _latest_artifact_for_proposal(proposal_id)
    if not art:
        return "⛔ No compiled artifact for this proposal. Run /improve_compile <proposal_id> first."

    # 0) artifact validation gate (typed, safe scope)
    art_ok = True
    art_note = "ok"
    try:
        ajson = json.loads(art["artifact_json"] or "{}")
        atype = (art["artifact_type"] or "").strip()
        target = (art["target"] or "").strip()
        c_ok, c_note = _artifact_has_expected_contract(atype, target, (ajson or {}))
        if not c_ok:
            art_ok = False
            art_note = c_note
        elif atype == "routing_rule" and target == "unknown_command_handler":
            aliases = (ajson or {}).get("add_alias", {})
            art_ok = isinstance(aliases, dict) and all(bool(str(k).strip()) and bool(str(v).strip()) for k, v in aliases.items())
            if not art_ok:
                art_note = "invalid alias map"
        elif atype == "config_change" and target == "runtime_retry_policy":
            key = (ajson or {}).get("key")
            val = (ajson or {}).get("value")
            art_ok = (key == "exec_retry_limit") and str(val).isdigit() and (0 <= int(val) <= 3)
            if not art_ok:
                art_note = "invalid retry policy artifact"
        elif atype == "state_lineage" and target == "runtime_snapshot":
            aid = (ajson or {}).get("artifact_id")
            art_ok = str(aid).isdigit() and int(aid) >= 0
            if not art_ok:
                art_note = "invalid lineage artifact"
        elif atype == "prompt_change" and target == "runtime_prompt":
            mode = str((ajson or {}).get("mode", "")).strip().lower()
            val = str((ajson or {}).get("value", ""))
            art_ok = mode in {"strict", "normal", "overseer"} and len(val.strip()) >= 40
            if not art_ok:
                art_note = "invalid prompt artifact"
        elif atype == "skill_policy_change" and target == "runtime_skill_config":
            skill = str((ajson or {}).get("skill", "")).strip()
            policy = str((ajson or {}).get("policy", "")).strip().lower()
            val = str((ajson or {}).get("value", "")).strip()
            allowed = {"max_calls_per_hour", "allow_domains", "write_root", "default_cwd", "allow_raw_shell"}
            art_ok = bool(skill) and policy in allowed and bool(val)
            if not art_ok:
                art_note = "invalid skill policy artifact"
        else:
            art_ok = False
            art_note = f"unsupported artifact scope: {atype}:{target}"
    except Exception as exc:
        art_ok = False
        art_note = str(exc)[:300]
    checks.append(("artifact_validation", art_ok, art_note))

    # 1) compile gate
    try:
        proc = subprocess.run(
            [str(py), "-m", "py_compile", "XoBop.py"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=60,
        )
        ok = proc.returncode == 0
        note = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[:300]
        checks.append(("compile", ok, note or "ok"))
    except Exception as exc:
        checks.append(("compile", False, str(exc)[:300]))

    # 2) deterministic eval suite gate
    try:
        proc = subprocess.run(
            [str(py), "scripts/eval_suite.py"],
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=180,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        ok = proc.returncode == 0 and ("RESULT: PASS" in out)
        checks.append(("eval_suite", ok, out[-300:] if out else "ok"))
    except Exception as exc:
        checks.append(("eval_suite", False, str(exc)[:300]))

    # 3) regression replay (router deterministic samples)
    replay_ok = True
    replay_note = "ok"
    try:
        samples = [
            ("/cronlist", "cron_list"),
            ("how many files in downloads", "download_count"),
            ("did you send the email?", "email_status"),
            ("git status", "direct_shell"),
        ]
        bad = []
        for text, expected in samples:
            got = router_classify(text)["name"]
            if got != expected:
                bad.append(f"{text} => {got} (expected {expected})")
        if bad:
            replay_ok = False
            replay_note = "; ".join(bad)[:300]
    except Exception as exc:
        replay_ok = False
        replay_note = str(exc)[:300]
    checks.append(("replay_regression", replay_ok, replay_note))

    # 4) prompt golden replay gate for prompt artifacts
    if (art["artifact_type"] or "") == "prompt_change" and (art["target"] or "") == "runtime_prompt":
        prompt_ok = True
        prompt_note = "ok"
        try:
            pj = json.loads(art["artifact_json"] or "{}")
            ptxt = str((pj or {}).get("value", ""))
            golden_prompt_checks = [
                ("contains safety", "safety" in ptxt.lower() or "safe" in ptxt.lower()),
                ("contains tool guidance", "tool" in ptxt.lower()),
                ("non-empty", len(ptxt.strip()) >= 40),
            ]
            bad = [name for name, ok in golden_prompt_checks if not ok]
            if bad:
                prompt_ok = False
                prompt_note = "missing: " + ", ".join(bad)
        except Exception as exc:
            prompt_ok = False
            prompt_note = str(exc)[:300]
        checks.append(("prompt_golden_replay", prompt_ok, prompt_note))

    # 5) skill-policy simulation cases for skill_policy artifacts
    if (art["artifact_type"] or "") == "skill_policy_change" and (art["target"] or "") == "runtime_skill_config":
        sp_ok = True
        sp_note = "ok"
        try:
            pj = json.loads(art["artifact_json"] or "{}")
            bad = []
            for c in skill_policy_simulation_cases():
                ctype = c.get("type")
                field = str(c.get("field", ""))
                val = str((pj or {}).get(field, ""))
                ok = True
                if ctype == "non_empty":
                    ok = bool(val.strip())
                elif ctype == "allowed_values":
                    ok = val.strip().lower() in {str(x).strip().lower() for x in c.get("allowed", [])}
                if bool(c.get("expect_ok", True)) != bool(ok):
                    bad.append(f"{c.get('case_id')}: {field}={val!r}")
            if bad:
                sp_ok = False
                sp_note = "; ".join(bad)[:300]
        except Exception as exc:
            sp_ok = False
            sp_note = str(exc)[:300]
        checks.append(("skill_policy_simulation", sp_ok, sp_note))

    shell_replay_report = None
    if USE_SHELL_REPLAY_CONTROLLER:
        run_id = f"sim_{proposal_id}_{int(time.time())}"
        tmp_root = Path("/tmp/xobop_sim")
        dbm = TempDbManager(DB_FILE, str(tmp_root))
        temp_db = dbm.fork(run_id)
        sandbox_root = str(tmp_root / run_id / "sandbox")
        try:
            rc = ReplayController(command_policy_checker=_sim_shell_policy_check, diff_builder=_build_shell_replay_diff)
            art_obj = {
                "id": int(art["id"]),
                "artifact_type": art["artifact_type"],
                "target": art["target"],
                "artifact_json": art["artifact_json"],
            }
            shell_replay_report = rc.run(
                proposal_id=proposal_id,
                artifact=art_obj,
                replay_cases=shell_replay_cases(),
                golden_cases=shell_golden_cases(),
                temp_db_path=temp_db,
                sandbox_root=sandbox_root,
                apply_artifact_to_temp_state=apply_artifact_to_temp_settings,
            )
            replay_ok2 = (shell_replay_report.get("status") == "pass")
            checks.append(("shell_replay_controller", replay_ok2, json.dumps(shell_replay_report.get("summary", {}))[:300]))
        except Exception as exc:
            checks.append(("shell_replay_controller", False, str(exc)[:300]))
        finally:
            dbm.cleanup(run_id)

    overall = all(ok for _, ok, _ in checks)

    with get_db() as conn:
        conn.execute(
            "UPDATE change_artifacts SET status=?, simulated_ts=CURRENT_TIMESTAMP WHERE id=?",
            ("sim_pass" if overall else "sim_fail", int(art["id"])),
        )
        row = conn.execute(
            "SELECT risk_level,title FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Proposal not found: {proposal_id}")
        before = {"proposal_id": proposal_id, "risk_level": row["risk_level"], "title": row["title"], "artifact_id": int(art["id"]), "artifact_type": art["artifact_type"]}
        after = {k: {"ok": ok, "note": note} for k, ok, note in checks}
        if shell_replay_report is not None:
            after["shell_replay_report"] = shell_replay_report
        cur = conn.execute(
            """
            INSERT INTO improvement_runs(proposal_id,phase,status,eval_phase,before_json,after_json,notes)
            VALUES(?, 'simulation', ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                "ok" if overall else "error",
                _get_eval_phase(),
                json.dumps(before),
                json.dumps(after),
                "simulation gate",
            ),
        )
        run_id = int(cur.lastrowid)
        if shell_replay_report is not None:
            for r in shell_replay_report.get("replay_results", []):
                exp_ok = bool(r.get("expect_ok", True))
                act_ok = bool(r.get("actual_ok", False))
                conn.execute(
                    """
                    INSERT INTO simulation_case_results(run_id,proposal_id,case_id,case_class,expected,actual,improved,regressed,blocked,policy_ok)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        proposal_id,
                        str(r.get("case_id", "")),
                        str(r.get("case_class", "targeted")),
                        ("ok" if exp_ok else "fail"),
                        ("ok" if act_ok else "fail"),
                        int(exp_ok == act_ok),
                        int(exp_ok != act_ok),
                        int(not act_ok),
                        int(bool(r.get("policy_ok", False))),
                    ),
                )
            for g in shell_replay_report.get("golden_results", []):
                exp_ok = bool(g.get("expected_ok", True))
                act_ok = bool(g.get("actual_ok", False))
                conn.execute(
                    """
                    INSERT INTO simulation_case_results(run_id,proposal_id,case_id,case_class,expected,actual,improved,regressed,blocked,policy_ok)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        proposal_id,
                        str(g.get("case_id", "")),
                        "golden",
                        ("ok" if exp_ok else "fail"),
                        ("ok" if act_ok else "fail"),
                        int(exp_ok == act_ok),
                        int(exp_ok != act_ok),
                        int(not act_ok),
                        int(exp_ok == act_ok),
                    ),
                )

    lines = [f"Simulation for proposal #{proposal_id}: {'PASS' if overall else 'FAIL'}"]
    for name, ok, note in checks:
        lines.append(f"• {name}: {'✅' if ok else '❌'}")
        if note and note != "ok":
            lines.append(f"  - {note}")
    return "\n".join(lines)


def _impact_compare_sync(proposal_id: int, window_days: int = 7) -> str:
    return ipo_impact_compare_sync(proposal_id=proposal_id, window_days=window_days, get_db=get_db, get_eval_phase=_get_eval_phase)
def _latest_run_status(proposal_id: int, phase: str) -> Optional[str]:
    return ipo_latest_run_status(proposal_id=proposal_id, phase=phase, get_db=get_db)
def _latest_impact_delta(proposal_id: int) -> dict[str, float]:
    return ipo_latest_impact_delta(proposal_id=proposal_id, get_db=get_db)
def _latest_impact_confidence(proposal_id: int) -> str:
    return ipo_latest_impact_confidence(proposal_id=proposal_id, get_db=get_db)
def _promote_proposal_sync(proposal_id: int, force: bool = False, reason: str = "") -> str:
    import subprocess

    policy = _get_improve_policy()
    phase = _get_eval_phase()
    promotion_mode = "force" if force else "policy"
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,status,risk_level,title FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

    if p["status"] in {"promoted", "rejected"}:
        return f"Proposal #{proposal_id} already {p['status']}."

    if force and not (reason or "").strip():
        return "⛔ Force promotion requires a reason. Use: /improve_promote <id> force <reason>"

    if phase == "baseline" and not force:
        return "⛔ Eval phase is baseline: promotion disabled (use force only for emergency)."

    art = _latest_artifact_for_proposal(proposal_id)
    if not art:
        return "⛔ No compiled artifact. Run /improve_compile <proposal_id> first."

    risk = (p["risk_level"] or "low").lower()
    sim = _latest_run_status(proposal_id, "simulation")
    imp = _latest_run_status(proposal_id, "impact_compare")

    if sim != "ok":
        return "⛔ Promotion blocked: simulation gate is not PASS. Run /improve_simulate first."

    if policy == "manual" and not force:
        return "⛔ Policy=manual. Re-run with force for explicit approval."

    if risk == "high" and not force:
        return "⛔ High-risk proposal requires explicit force approval."
    if risk == "medium" and policy in {"manual", "assisted"} and not force:
        return "⛔ Medium-risk proposal requires force under current policy."

    if art["artifact_type"] == "prompt_change" and art["target"] == "runtime_prompt" and not force:
        return "⛔ Prompt artifacts require force approval due to global behavior blast radius."

    if imp == "error" and not force:
        return "⛔ Impact comparison indicates regression. Use force only with explicit acceptance."

    if imp == "ok" and not force:
        conf = _latest_impact_confidence(proposal_id)
        if conf == "low":
            return "⛔ Impact comparison confidence is LOW (< 20 samples before or after). Wait for more telemetry or use force to override."

    # Net-benefit guard for retry policy artifacts
    if art["artifact_type"] == "config_change" and art["target"] == "runtime_retry_policy" and not force:
        d = _latest_impact_delta(proposal_id)
        if d:
            fail_delta = float(d.get("fail_rate", 0.0))
            lat_delta = float(d.get("avg_latency", 0.0))
            if not (fail_delta <= -1.0 and lat_delta <= 300.0):
                return "⛔ Retry artifact blocked by net-benefit rule (need fail_rate <= -1.0pp and avg_latency <= +300ms)."

    # Apply typed artifact (safe mutable scopes only)
    ajson = json.loads(art["artifact_json"] or "{}")
    atype = art["artifact_type"]
    target = art["target"]
    contract_ok, contract_note = _artifact_has_expected_contract(atype, target, (ajson or {}))
    if not contract_ok:
        return f"⛔ Artifact apply failed: {contract_note}."

    pre_state = _runtime_state_payload(last_promotion_mode="")
    commit_hash = "unknown"
    repo_dir = Path(__file__).resolve().parent
    try:
        applied_desc = _apply_artifact_to_runtime_settings(atype, target, (ajson or {}))
        _set_last_applied_artifact_id(int(art["id"]))
        _export_runtime_snapshot(last_applied_artifact_id=int(art["id"]), last_promotion_mode=promotion_mode)

        try:
            rel_snap = os.path.relpath(str(RUNTIME_SNAPSHOT_FILE), str(repo_dir))
            subprocess.run(["git", "-C", str(repo_dir), "add", rel_snap], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", f"self-mod: apply artifact #{int(art['id'])} for proposal #{proposal_id}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            commit_hash = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"], text=True).strip() or "unknown"
        except Exception:
            pass

        if force:
            _record_op_event(
                "force_promotion",
                route="improve_promote",
                status="ok",
                metadata={
                    "proposal_id": proposal_id,
                    "artifact_id": int(art["id"]),
                    "reason": (reason or "unspecified"),
                    "eval_phase": phase,
                },
            )

        with get_db() as conn:
            conn.execute(
                "UPDATE improvement_proposals SET status='promoted' WHERE id=?",
                (proposal_id,),
            )
            conn.execute(
                "UPDATE change_artifacts SET status='applied', applied_ts=CURRENT_TIMESTAMP, commit_hash=?, promotion_mode=?, eval_phase=? WHERE id=?",
                (commit_hash, promotion_mode, phase, int(art["id"])),
            )
            conn.execute(
                """
                INSERT INTO improvement_runs(proposal_id,phase,status,promotion_mode,eval_phase,reason,before_json,after_json,notes)
                VALUES(?, 'promotion', 'ok', ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    promotion_mode,
                    phase,
                    (reason or ""),
                    json.dumps({"artifact_id": int(art["id"]), "artifact_type": atype, "target": target}),
                    json.dumps({"applied": applied_desc}),
                    f"policy={policy}; phase={phase}; risk={risk}; force={int(force)}; artifact_id={int(art['id'])}; commit={commit_hash}",
                ),
            )
    except Exception as exc:
        # best-effort runtime rollback if promotion bookkeeping fails midway
        try:
            _set_command_aliases(dict(pre_state.get("command_aliases", {})))
            _set_exec_retry_limit(int(pre_state.get("exec_retry_limit", 0) or 0))
            _set_last_applied_artifact_id(int(pre_state.get("last_applied_artifact_id", 0) or 0))
            _export_runtime_snapshot(
                last_applied_artifact_id=int(pre_state.get("last_applied_artifact_id", 0) or 0),
                last_promotion_mode=str(pre_state.get("last_promotion_mode", "") or ""),
            )
        except Exception:
            pass
        return f"⛔ Promotion failed and runtime state was rolled back: {str(exc)[:200]}"

    return f"✅ Proposal #{proposal_id} promoted+applied under policy={policy} risk={risk} artifact={atype}:{target} commit={commit_hash}."


def _auto_promote_low_sync() -> str:
    return ipo_auto_promote_low_sync(get_improve_policy=_get_improve_policy, get_db=get_db, promote_proposal_sync=_promote_proposal_sync)
def _run_pipeline_sweep(max_proposals: int = 10) -> str:
    return ipo_run_pipeline_sweep(
        get_db=get_db,
        compile_proposal_to_change=_compile_proposal_to_change,
        simulate_proposal_sync=_simulate_proposal_sync,
        auto_promote_low_sync=_auto_promote_low_sync,
        max_proposals=max_proposals,
    )

# xobop_core_base.py functions (e.g. the auto-improve tick handler and
# _run_cron_job) are defined with base's own module globals, so they can't
# see names defined only here in tail. Inject the callables base needs to
# invoke back into base's namespace. Without this, _promote_proposal_sync
# was already unreachable from the auto-improve autopromote branch
# (xobop_core_base.py's "if autopromote_enabled" block) -- it always
# NameError'd and was silently swallowed by that block's try/except.
_base._promote_proposal_sync = _promote_proposal_sync
_base._run_pipeline_sweep = _run_pipeline_sweep
def _capture_evolution_memory_sync(proposal_id: int) -> str:
    return ipo_capture_evolution_memory_sync(proposal_id=proposal_id, get_db=get_db)
def _recapture_all_evolution_sync() -> str:
    return ipo_recapture_all_evolution_sync(get_db=get_db, capture_fn=_capture_evolution_memory_sync)
def _evolution_memory_report(limit: int = 10) -> str:
    return ipo_evolution_memory_report(limit=limit, get_db=get_db)
def _lookup_evolution_memory(failure_class: str, target_route: str = "") -> list:
    return ipo_lookup_evolution_memory(get_db=get_db, failure_class=failure_class, target_route=target_route)
def _improve_funnel_report(days: int = 30) -> str:
    return ipo_improve_funnel_report(days=days, get_db=get_db)
def _improve_weekly_report(weeks: int = 4) -> str:
    return ipo_improve_weekly_report(weeks=weeks, get_db=get_db, get_eval_phase=_get_eval_phase, get_improve_policy=_get_improve_policy)
def _improve_eval_report(days: int = 30) -> str:
    return ipo_improve_eval_report(
        days=days,
        get_db=get_db,
        get_eval_phase=_get_eval_phase,
        get_improve_policy=_get_improve_policy,
        improve_funnel_report_fn=_improve_funnel_report,
        improve_weekly_report_fn=_improve_weekly_report,
    )
def _op_sanity_report(hours: int = 24) -> str:
    h = max(1, min(hours, 168))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) GROUP BY event_type ORDER BY n DESC",
            (f"-{h} hour",),
        ).fetchall()
        per_hour = conn.execute(
            "SELECT strftime('%Y-%m-%d %H', timestamp) AS hr, COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) GROUP BY hr ORDER BY hr",
            (f"-{h} hour",),
        ).fetchall()
        dup = conn.execute(
            """
            SELECT event_type,command,route,COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
            GROUP BY event_type,command,route,chat_id,strftime('%Y-%m-%d %H:%M', timestamp)
            HAVING COUNT(*) > 3
            ORDER BY n DESC LIMIT 10
            """,
            (f"-{h} hour",),
        ).fetchall()
        requested = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='command_requested' AND timestamp >= datetime('now', ?)", (f"-{h} hour",)).fetchone()["n"]
        routed = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='route_selected' AND timestamp >= datetime('now', ?)", (f"-{h} hour",)).fetchone()["n"]
        tools = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='tool_run' AND timestamp >= datetime('now', ?)", (f"-{h} hour",)).fetchone()["n"]

    anomalies = []
    if requested > 0 and routed < int(0.6 * requested):
        anomalies.append(f"low route coverage: requested={requested} routed={routed}")
    if requested > 0 and tools > requested * 5:
        anomalies.append(f"tool/request ratio high: tools={tools} requested={requested}")
    if dup:
        anomalies.append(f"duplicate bursts: {len(dup)} groups")
    if not rows:
        anomalies.append("no op_events in window")

    lines = [f"Op sanity ({h}h)"]
    lines.append("Events per type:")
    for r in rows[:10]:
        lines.append(f"- {r['event_type']}: {int(r['n'])}")
    lines.append(f"Hourly buckets: {len(per_hour)}")
    lines.append(f"Missing sequence hints: requested={int(requested)} routed={int(routed)} tool_run={int(tools)}")
    if dup:
        lines.append("Duplicate groups:")
        for d in dup[:5]:
            lines.append(f"- {d['event_type']} cmd={d['command'] or '-'} route={d['route'] or '-'} n={int(d['n'])}")
    lines.append("Anomalies:")
    if anomalies:
        lines.extend([f"- {a}" for a in anomalies])
    else:
        lines.append("- none")
    return "\n".join(lines)


def _top_skills(limit: int = 10) -> str:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT skill_name, call_count, error_count,
                   CASE WHEN call_count > 0 THEN total_duration_ms * 1.0 / call_count ELSE 0 END AS avg_ms,
                   last_used_ts
            FROM skill_usage_stats
            ORDER BY call_count DESC, last_used_ts DESC
            LIMIT ?
            """,
            (max(1, min(limit, 20)),),
        ).fetchall()
    if not rows:
        return "No skill usage yet."
    return "\n".join(
        f"• *{r['skill_name']}*: calls={r['call_count']} errors={r['error_count']} avg={float(r['avg_ms']):.1f}ms"
        for r in rows
    )


# =============================================================================
# Telegram command handlers
# =============================================================================


def _owner_only(update: Update) -> bool:
    return owner_only_impl(update=update, owner_id=OWNER_ID)


async def _reply(update: Update, text: str) -> None:
    text = normalize_machine_kv_output(text)
    await reply_impl(
        update=update,
        text=text,
        asyncio_mod=asyncio,
        fire_and_forget=_fire_and_forget,
        save_message=_save_message,
        telegram_error_cls=TelegramError,
        log=log,
    )


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_remember_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        save_fact=_save_fact,
        save_fact_memory=_save_fact_memory,
    )


async def cmd_delfact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_delfact_impl(update=update, owner_only=_owner_only, reply=_reply, delete_fact=_delete_fact)


async def cmd_addtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_addtask_impl(update=update, owner_only=_owner_only, reply=_reply, add_task=_add_task)


async def cmd_donetask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_donetask_impl(update=update, owner_only=_owner_only, reply=_reply, close_task=_close_task)


async def cmd_facts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_facts_impl(update=update, owner_only=_owner_only, reply=_reply, get_facts=_get_facts)


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_tasks_impl(update=update, owner_only=_owner_only, reply=_reply, get_tasks=_get_tasks)


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_mode_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        modes=MODES,
        set_setting=_set_setting,
        get_mode=_get_mode,
        to_thread=_to_thread,
    )


async def cmd_promptshow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_promptshow_impl(update=update, owner_only=_owner_only, reply=_reply, get_mode=_get_mode, get_runtime_prompt=_get_runtime_prompt)


async def cmd_promptset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_promptset_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        modes=MODES,
        set_runtime_prompt=_set_runtime_prompt,
        strict_prompt=STRICT_PROMPT,
        normal_prompt=NORMAL_PROMPT,
        to_thread=_to_thread,
    )


async def cmd_quiethours(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_quiethours_impl(update=update, owner_only=_owner_only, reply=_reply, get_setting=_get_setting, set_setting=_set_setting)


async def cmd_quietoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_quietoff_impl(update=update, owner_only=_owner_only, reply=_reply, set_setting=_set_setting)


async def cmd_quietflush(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_quietflush_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db, client=client, model=MODEL)


async def cmd_promptproposal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_promptproposal_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        modes=MODES,
        get_prompt_proposal=_get_prompt_proposal,
        set_prompt_proposal=_set_prompt_proposal,
    )


async def cmd_promptapprove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_promptapprove_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_prompt_proposal=_get_prompt_proposal,
        set_prompt_proposal=_set_prompt_proposal,
        set_runtime_prompt=_set_runtime_prompt,
    )


async def cmd_promptreject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_promptreject_impl(update=update, owner_only=_owner_only, reply=_reply, set_prompt_proposal=_set_prompt_proposal)


def _run_checkup_sync() -> str:
    return run_checkup_sync_impl(system_pulse=_system_pulse)


def _generate_system_map_sync() -> str:
    return generate_system_map_sync_impl(cwd=os.getcwd())


async def cmd_checkup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_checkup_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        asyncio_mod=asyncio,
        run_checkup_sync=_run_checkup_sync,
        record_op_event=_record_op_event,
    )


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_plan_impl(update=update, owner_only=_owner_only, reply=_reply, state=STATE)


async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_go_impl(update=update, owner_only=_owner_only, reply=_reply, state=STATE, run_agent=_run_agent)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_status_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        model=MODEL,
        embedding_model=EMBEDDING_MODEL,
        get_mode=_get_mode,
        get_db=get_db,
    )


async def cmd_procinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_procinfo_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        token=TOKEN,
        get_mode=_get_mode,
        get_setting=_get_setting,
    )


async def cmd_attachdiag(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_attachdiag_impl(update=update, owner_only=_owner_only, reply=_reply, state=STATE)


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _owner_only(update):
        await _reply(update, _health_report())


async def cmd_state_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_state_check_impl(update=update, owner_only=_owner_only, reply=_reply, state_check_dict=_state_check_dict)


async def cmd_state_repair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_state_repair_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        repair_state_from_snapshot=_repair_state_from_snapshot,
        record_op_event=_record_op_event,
    )


async def cmd_state_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_state_clear_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        clear_workflow_state_for_chat=_clear_workflow_state_for_chat,
    )


async def cmd_op_sanity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_op_sanity_impl(update=update, owner_only=_owner_only, reply=_reply, op_sanity_report=_op_sanity_report)


async def cmd_diagnose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_diagnose_impl(update=update, owner_only=_owner_only, reply=_reply, diagnose_report=_diagnose_report)


async def cmd_improve_propose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_propose_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        generate_improvement_proposal=_generate_improvement_proposal,
        record_op_event=_record_op_event,
        diagnose_report_fn=_diagnose_report,
        embed_text_fn=_embed_text,
        fetch_conditioned_evolution_async=ipo_fetch_conditioned_evolution_async,
        get_db=get_db,
    )


async def cmd_improve_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_list_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db)


async def cmd_improve_compile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_compile_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        compile_proposal_to_change=_compile_proposal_to_change,
    )


async def cmd_improve_artifacts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_artifacts_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db)


async def cmd_improve_simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_simulate_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        simulate_proposal_sync=_simulate_proposal_sync,
    )


async def cmd_improve_runs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_runs_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db)


async def cmd_improve_compare(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_compare_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        impact_compare_sync=_impact_compare_sync,
    )


async def cmd_improve_policy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_policy_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_setting=_get_setting,
        get_improve_policy=_get_improve_policy,
        set_setting=_set_setting,
    )


async def cmd_improve_promote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_promote_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        promote_proposal_sync=_promote_proposal_sync,
    )


async def cmd_improve_autopromote(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_autopromote_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        auto_promote_low_sync=_auto_promote_low_sync,
    )


async def cmd_improve_auto_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_auto_status_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_setting=_get_setting,
    )


async def cmd_improve_auto_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_auto_on_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        set_setting=_set_setting,
    )


async def cmd_improve_auto_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_auto_off_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        set_setting=_set_setting,
    )


async def cmd_improve_auto_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_auto_last_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_db=get_db,
    )


async def cmd_improve_auto_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_auto_dashboard_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_db=get_db,
    )


async def cmd_improve_funnel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_funnel_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        improve_funnel_report=_improve_funnel_report,
    )


async def cmd_improve_eval_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_eval_report_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        improve_eval_report=_improve_eval_report,
    )


async def cmd_improve_weekly_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_weekly_report_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        improve_weekly_report=_improve_weekly_report,
    )


async def cmd_improve_phase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_phase_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_eval_phase=_get_eval_phase,
        set_setting=_set_setting,
    )


async def cmd_improve_log_rollback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_log_rollback_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        record_op_event=_record_op_event,
    )


async def cmd_improve_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_reject_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_db=get_db,
    )


async def cmd_improve_set_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_set_retry_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        get_exec_retry_limit=_get_exec_retry_limit,
        set_exec_retry_limit=_set_exec_retry_limit,
        export_snapshot_fn=_export_runtime_snapshot,
    )


async def cmd_evolve_capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_capture_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        capture_evolution_memory_sync=_capture_evolution_memory_sync,
        embed_evolution_entry_async=lambda pid, embed_fn: ipo_embed_evolution_entry_async(
            proposal_id=pid, get_db=get_db, embed_fn=embed_fn
        ),
        embed_fn=_embed_text,
    )


async def cmd_evolve_recapture_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_recapture_all_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        recapture_all_evolution_sync=_recapture_all_evolution_sync,
    )


async def cmd_evolve_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_memory_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        evolution_memory_report=_evolution_memory_report,
    )


async def cmd_evolve_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_status_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db)


async def cmd_evolve_blocks_rebuild(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_blocks_rebuild_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        recompute_evolution_blocks=ipo_recompute_evolution_blocks,
        get_db=get_db,
    )


async def cmd_topskills(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_topskills_impl(update=update, owner_only=_owner_only, reply=_reply, top_skills=_top_skills)


async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memories_impl(update=update, owner_only=_owner_only, reply=_reply, list_memories=_list_memories)


async def cmd_memadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memadd_impl(update=update, owner_only=_owner_only, reply=_reply, add_memory=_add_memory)


async def cmd_memdel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memdel_impl(update=update, owner_only=_owner_only, reply=_reply, delete_memory_sync=_delete_memory_sync)


async def cmd_memdedup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memdedup_impl(update=update, owner_only=_owner_only, reply=_reply, memdedup=_memdedup_sync)


async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_remind_impl(
        update=update, owner_only=_owner_only, reply=_reply,
        add_reminder=_add_reminder, parse_when=rt_parse_reminder_when, utcnow=datetime.utcnow,
    )


async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_reminders_impl(update=update, owner_only=_owner_only, reply=_reply, list_reminders=_list_reminders)


async def cmd_remindcancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_remindcancel_impl(update=update, owner_only=_owner_only, reply=_reply, cancel_reminder=_cancel_reminder)


def _new_remind_job_name() -> str:
    return f"remind-{int(time.time() * 1000)}"


async def cmd_remindevery(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_remindevery_impl(
        update=update, ctx=ctx, owner_only=_owner_only, reply=_reply,
        parse_cron_expr=_parse_cron_expr, parse_nl_to_cron=_parse_nl_to_cron,
        add_cron_job=_add_cron_job, sync_scheduler=_sync_scheduler,
        new_job_name=_new_remind_job_name, max_text_len=MAX_REMINDER_TEXT,
    )


async def cmd_memdiag(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memdiag_impl(update=update, owner_only=_owner_only, reply=_reply, memory_diag=_memory_diag)


async def cmd_exec(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_exec_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        state=STATE,
        get_mode=_get_mode,
        call_skill=_call_skill,
        log=log,
        chat_action_typing=lambda u: u.message.chat.send_action(ChatAction.TYPING),
    )


async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_skills_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db)


async def cmd_skillon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_skillon_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db, audit_skill=_audit_skill)


async def cmd_skilloff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_skilloff_impl(update=update, owner_only=_owner_only, reply=_reply, get_db=get_db, audit_skill=_audit_skill)


async def cmd_cronadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_cronadd_impl(
        update=update,
        ctx=ctx,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        parse_cron_expr=_parse_cron_expr,
        parse_nl_to_cron=_parse_nl_to_cron,
        add_cron_job=_add_cron_job,
        sync_scheduler=_sync_scheduler,
    )


async def cmd_cronrm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_cronrm_impl(
        update=update,
        ctx=ctx,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        remove_cron_job=_remove_cron_job,
    )


async def cmd_cronlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_cronlist_impl(update=update, owner_only=_owner_only, reply=_reply, list_cron_jobs=_list_cron_jobs)


async def cmd_cronon(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_cronon_impl(
        update=update,
        ctx=ctx,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        toggle_cron_job=_toggle_cron_job,
        sync_scheduler=_sync_scheduler,
    )


async def cmd_cronoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_cronoff_impl(
        update=update,
        ctx=ctx,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        toggle_cron_job=_toggle_cron_job,
    )


async def cmd_cronruns(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_cronruns_impl(update=update, owner_only=_owner_only, reply=_reply, get_cron_runs=_get_cron_runs)


async def cmd_cronrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async def _run_cron_job_background(name: str):
        asyncio.create_task(_run_cron_job(name))

    await cmd_cronrun_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        get_db=get_db,
        run_cron_job=_run_cron_job_background,
        chat_action_typing=lambda u: u.message.chat.send_action(ChatAction.TYPING),
    )


async def cmd_clearctx(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    history_by_chat[update.effective_chat.id] = []
    await _reply(update, "Context cleared.")


async def cmd_panicstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_panicstatus_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        panic_locked=_panic_locked,
        get_setting=_get_setting,
    )


async def cmd_paniclock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_paniclock_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        now_ts=int(time.time()),
        set_setting=_set_setting,
        get_setting=_get_setting,
        uuid_hex12=uuid.uuid4().hex[:12],
    )


async def cmd_panicunlock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_panicunlock_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        panic_unlock_code_getter=lambda: (os.getenv("PANIC_UNLOCK_CODE", "") or _get_setting("panic_unlock_code", "") or PANIC_UNLOCK_CODE),
        set_setting=_set_setting,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help_impl(update=update, owner_only=_owner_only, reply=_reply)


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    dispatch = build_unknown_dispatch(command_dispatch=_COMMAND_DISPATCH, ctx=ctx)
    await ah_cmd_unknown_impl(
        update=update,
        owner_only=_owner_only,
        get_command_aliases=_get_command_aliases,
        resolve_command_alias=resolve_command_alias,
        command_dispatch=dispatch,
        record_op_event=_record_op_event,
        reply=_reply,
    )


# intent and email helpers moved to xobop/intents.py and xobop/emailer.py


async def handle_media_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await ah_handle_media_message_impl(
        update=update,
        process_media_message_fn=build_media_processor(
            process_media_message_fn=process_media_message,
            state=STATE,
            artifact_dir=str(ARTIFACT_DIR),
            owner_only=_owner_only,
            reply=_reply,
            log=log,
        ),
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    command_registry = {
        "/cronadd": (lambda u2: cmd_cronadd(u2, ctx), None, None),
        "/cronlist": (lambda u2: cmd_cronlist(u2, ctx), None, None),
        "/cronrun": (lambda u2: cmd_cronrun(u2, ctx), None, None),
        "/cronruns": (lambda u2: cmd_cronruns(u2, ctx), None, None),
        "/status": (lambda u2: cmd_status(u2, ctx), "route_selected", "cmd_status"),
        "/health": (lambda u2: cmd_health(u2, ctx), "route_selected", "cmd_health"),
        "/checkup": (lambda u2: cmd_checkup(u2, ctx), "health_check", "cmd_checkup"),
        "/state_check": (lambda u2: cmd_state_check(u2, ctx), "route_selected", "cmd_state_check"),
        "/state_repair": (lambda u2: cmd_state_repair(u2, ctx), "route_selected", "cmd_state_repair"),
        "/state_clear": (lambda u2: cmd_state_clear(u2, ctx), "route_selected", "cmd_state_clear"),
        "/op_sanity": (lambda u2: cmd_op_sanity(u2, ctx), "route_selected", "cmd_op_sanity"),
        "/diagnose": (lambda u2: cmd_diagnose(u2, ctx), "route_selected", "cmd_diagnose"),
        "/improve_propose": (lambda u2: cmd_improve_propose(u2, ctx), "route_selected", "cmd_improve_propose"),
        "/improve_list": (lambda u2: cmd_improve_list(u2, ctx), "route_selected", "cmd_improve_list"),
        "/improve_compile": (lambda u2: cmd_improve_compile(u2, ctx), "route_selected", "cmd_improve_compile"),
        "/improve_artifacts": (lambda u2: cmd_improve_artifacts(u2, ctx), "route_selected", "cmd_improve_artifacts"),
        "/improve_simulate": (lambda u2: cmd_improve_simulate(u2, ctx), "route_selected", "cmd_improve_simulate"),
        "/improve_runs": (lambda u2: cmd_improve_runs(u2, ctx), "route_selected", "cmd_improve_runs"),
        "/improve_compare": (lambda u2: cmd_improve_compare(u2, ctx), "route_selected", "cmd_improve_compare"),
        "/improve_policy": (lambda u2: cmd_improve_policy(u2, ctx), "route_selected", "cmd_improve_policy"),
        "/improve_promote": (lambda u2: cmd_improve_promote(u2, ctx), "route_selected", "cmd_improve_promote"),
        "/improve_autopromote": (lambda u2: cmd_improve_autopromote(u2, ctx), "route_selected", "cmd_improve_autopromote"),
        "/improve_auto_status": (lambda u2: cmd_improve_auto_status(u2, ctx), "route_selected", "cmd_improve_auto_status"),
        "/improve_auto_on": (lambda u2: cmd_improve_auto_on(u2, ctx), "route_selected", "cmd_improve_auto_on"),
        "/improve_auto_off": (lambda u2: cmd_improve_auto_off(u2, ctx), "route_selected", "cmd_improve_auto_off"),
        "/improve_auto_last": (lambda u2: cmd_improve_auto_last(u2, ctx), "route_selected", "cmd_improve_auto_last"),
        "/improve_auto_dashboard": (lambda u2: cmd_improve_auto_dashboard(u2, ctx), "route_selected", "cmd_improve_auto_dashboard"),
        "/improve_funnel": (lambda u2: cmd_improve_funnel(u2, ctx), "route_selected", "cmd_improve_funnel"),
        "/improve_eval_report": (lambda u2: cmd_improve_eval_report(u2, ctx), "route_selected", "cmd_improve_eval_report"),
        "/improve_weekly_report": (lambda u2: cmd_improve_weekly_report(u2, ctx), "route_selected", "cmd_improve_weekly_report"),
        "/improve_phase": (lambda u2: cmd_improve_phase(u2, ctx), "route_selected", "cmd_improve_phase"),
        "/improve_log_rollback": (lambda u2: cmd_improve_log_rollback(u2, ctx), "route_selected", "cmd_improve_log_rollback"),
        "/improve_reject": (lambda u2: cmd_improve_reject(u2, ctx), "route_selected", "cmd_improve_reject"),
        "/improve_set_retry": (lambda u2: cmd_improve_set_retry(u2, ctx), "route_selected", "cmd_improve_set_retry"),
        "/evolve_capture": (lambda u2: cmd_evolve_capture(u2, ctx), "route_selected", "cmd_evolve_capture"),
        "/evolve_recapture_all": (lambda u2: cmd_evolve_recapture_all(u2, ctx), "route_selected", "cmd_evolve_recapture_all"),
        "/evolve_memory": (lambda u2: cmd_evolve_memory(u2, ctx), "route_selected", "cmd_evolve_memory"),
        "/evolve_status": (lambda u2: cmd_evolve_status(u2, ctx), "route_selected", "cmd_evolve_status"),
        "/evolve_blocks_rebuild": (lambda u2: cmd_evolve_blocks_rebuild(u2, ctx), "route_selected", "cmd_evolve_blocks_rebuild"),
        "/panicstatus": (lambda u2: cmd_panicstatus(u2, ctx), None, None),
        "/paniclock": (lambda u2: cmd_paniclock(u2, ctx), None, None),
        "/panicunlock": (lambda u2: cmd_panicunlock(u2, ctx), None, None),
    }

    text_deps = ProcessTextDeps(
        state=STATE,
        reply=_reply,
        call_skill=_call_skill,
        mode_getter=_get_mode,
        list_cron_jobs=_list_cron_jobs,
        run_agent=_run_agent,
        owner_only=_owner_only,
        panic_locked=_panic_locked,
        panic_bypass_text=_is_panic_bypass_text,
        command_aliases_getter=_get_command_aliases,
        analyze_attachment=_analyze_attachment,
        op_event=_record_op_event,
        log=log,
        command_registry=command_registry,
    )

    async def _process_text(u, t, c):
        await process_text_message(update=u, text=t, chat_id=c, deps=text_deps)

    await ah_handle_message_impl(
        update=update,
        text=text,
        chat_id=chat_id,
        panic_locked=_panic_locked,
        panic_bypass_text=_is_panic_bypass_text,
        reply=_reply,
        record_op_event=_record_op_event,
        process_text_message_fn=_process_text,
        log=log,
    )


# =============================================================================
# Application setup
# =============================================================================

async def _post_init(app: Application) -> None:
    global _app, _state_task, _artifact_task, _notify_task, _memory_promo_task
    _app = app
    if any(t and not t.done() for t in (_state_task, _artifact_task, _notify_task, _memory_promo_task)):
        log.warning("post_init called with active background tasks; skipping duplicate task setup")
        return

    def _set_tasks(t1, t2, t3, t4):
        global _state_task, _artifact_task, _notify_task, _memory_promo_task
        _state_task, _artifact_task, _notify_task, _memory_promo_task = t1, t2, t3, t4

    await al_post_init_impl(
        app=app,
        build_scheduler=_build_scheduler,
        sync_scheduler=_sync_scheduler,
        log=log,
        load_session_state=_load_session_state,
        load_workflow_state=_load_workflow_state,
        state=STATE,
        notify_owner=_notify_owner,
        state_persist_loop=_state_persist_loop,
        artifact_watch_loop=_artifact_watch_loop,
        notification_flush_loop=_notification_flush_loop,
        memory_promotion_loop=_memory_promotion_loop,
        set_tasks=_set_tasks,
    )


async def _post_shutdown(app: Application) -> None:
    global _state_task, _artifact_task, _notify_task, _memory_promo_task

    def _clear_tasks():
        global _state_task, _artifact_task, _notify_task, _memory_promo_task
        _state_task = None
        _artifact_task = None
        _notify_task = None
        _memory_promo_task = None

    await al_post_shutdown_impl(
        app=app,
        log=log,
        tasks=(_state_task, _artifact_task, _notify_task, _memory_promo_task),
        clear_tasks=_clear_tasks,
        release_runtime_lock=_release_runtime_lock,
        wait_bg_futures=_wait_bg_futures,
    )


async def _graceful_shutdown(app: Application) -> None:
    await graceful_shutdown_impl(app=app, shutdown_impl=al_graceful_shutdown_impl, log=log)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    await on_error_impl(update=update, context=context, on_error_handler=al_on_error_impl, log=log)


def _apply_self_mods_to_runtime() -> None:
    apply_self_mods_impl(
        sm_apply_defaults=sm_apply_defaults,
        get_setting=_get_setting,
        set_setting=_set_setting,
        export_self_mods_debug=_export_self_mods_debug,
    )


def _startup_reconcile_runtime_state() -> None:
    return startup_reconcile_impl(
        load_snapshot=_load_runtime_snapshot,
        apply_self_mods_to_runtime=_apply_self_mods_to_runtime,
        export_runtime_snapshot_fn=_export_runtime_snapshot,
        get_last_applied_artifact_id=_get_last_applied_artifact_id,
        state_check_dict=_state_check_dict,
        repair_state_from_snapshot=_repair_state_from_snapshot,
        set_setting=_set_setting,
    )


# =============================================================================
# Cron skill_type dispatch table
#
# _run_cron_job lives in xobop_core_base.py, but several skill_types need
# names that only exist in this module's namespace (xobop_core_tail.py has
# full visibility into both, since it copies base's globals into itself at
# import time -- base never gets the reverse). Building the dispatch table
# here and injecting it into base, instead of an if/elif chain living in
# base itself, is what previously let _run_agent/_run_checkup_sync/
# _generate_system_map_sync/_promote_proposal_sync/_run_pipeline_sweep
# silently NameError for months: each new skill_type had to be manually
# checked for tail-only names. A dict built where every name is guaranteed
# resolvable removes that failure mode structurally.
# =============================================================================


async def _cron_handle_agent(job_name: str, skill_args: dict) -> str:
    query = skill_args.get("query", f"Run cron job: {job_name}")
    return await _run_agent(chat_id=0, query=query, force=True)


def _make_cron_skill_call_handler(skill_name: str):
    async def _handler(job_name: str, skill_args: dict) -> str:
        return await _call_skill(
            chat_id=0, user_query=f"cron:{job_name} force", mode="normal",
            skill_name=skill_name, args=skill_args,
        )
    return _handler


async def _cron_handle_memory_cleanup(job_name: str, skill_args: dict) -> str:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _prune_memory_sync)
    return "Memory cleanup completed."


async def _cron_handle_db_backup(job_name: str, skill_args: dict) -> str:
    backup_dir = skill_args.get("backup_dir", str(settings.backup_dir))
    repo_dir = skill_args.get("repo_dir", os.getcwd())
    loop = asyncio.get_running_loop()
    dst = await loop.run_in_executor(None, lambda: sqlite_online_backup(DB_FILE, backup_dir, repo_dir=repo_dir))
    return f"DB backup completed: {dst}"


async def _cron_handle_prompt_review(job_name: str, skill_args: dict) -> str:
    if not _overseer_enabled():
        return "Skipped prompt_review (overseer mode disabled)."
    with get_db() as conn:
        top = conn.execute("SELECT skill_name,call_count,error_count FROM skill_usage_stats ORDER BY error_count DESC, call_count DESC LIMIT 8").fetchall()
        recent = conn.execute("SELECT tool_name,status,result FROM runs ORDER BY id DESC LIMIT 30").fetchall()
    payload = {
        "top_skills": [dict(r) for r in top],
        "recent_runs": [dict(r) for r in recent],
    }
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Given skill usage and failures, propose a concise strict prompt improvement. Keep under 120 words."},
            {"role": "user", "content": json.dumps(payload)[:5000]},
        ],
        temperature=0.1,
        max_tokens=220,
    )
    proposal = (resp.choices[0].message.content or "").strip()
    global _prompt_proposal
    _prompt_proposal = {"mode": "strict", "text": proposal}
    return "Prompt proposal generated and staged. Use /promptapprove or /promptreject."


async def _cron_handle_checkup(job_name: str, skill_args: dict) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_checkup_sync)


async def _cron_handle_system_map_refresh(job_name: str, skill_args: dict) -> str:
    if not _overseer_enabled():
        return "Skipped system_map_refresh (overseer mode disabled)."
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(None, _generate_system_map_sync)
    return f"SYSTEM_MAP refreshed: {path}"


async def _cron_handle_evolve_blocks_recompute(job_name: str, skill_args: dict) -> str:
    n_blocks = int(skill_args.get("n_blocks", 4))
    written = await ipo_recompute_evolution_blocks(get_db=get_db, n_blocks=n_blocks)
    return f"Evolution memory blocks recomputed: {written} block(s) written."


async def _cron_handle_improve_pipeline_sweep(job_name: str, skill_args: dict) -> str:
    max_proposals = int(skill_args.get("max_proposals", 10))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _run_pipeline_sweep(max_proposals=max_proposals))


async def _cron_handle_reminders_check(job_name: str, skill_args: dict) -> str:
    return await ro_check_due_reminders(get_db=get_db, notify_owner=_notify_owner, utcnow=datetime.utcnow)


async def _cron_handle_daily_digest(job_name: str, skill_args: dict) -> str:
    loop = asyncio.get_running_loop()
    digest = await loop.run_in_executor(None, lambda: do_build_daily_digest(get_db=get_db))
    if digest:
        await _notify_owner(digest)
    return digest or "Nothing to report."


async def _cron_handle_notify(job_name: str, skill_args: dict) -> str:
    text = skill_args.get("text", "")
    await _notify_owner(f"⏰ Reminder: {text}")
    return "Notified owner."


_cron_skill_handlers: dict = {
    "agent": _cron_handle_agent,
    "memory_cleanup": _cron_handle_memory_cleanup,
    "db_backup": _cron_handle_db_backup,
    "prompt_review": _cron_handle_prompt_review,
    "checkup": _cron_handle_checkup,
    "system_map_refresh": _cron_handle_system_map_refresh,
    "evolve_blocks_recompute": _cron_handle_evolve_blocks_recompute,
    "improve_pipeline_sweep": _cron_handle_improve_pipeline_sweep,
    "reminders_check": _cron_handle_reminders_check,
    "daily_digest": _cron_handle_daily_digest,
    "notify": _cron_handle_notify,
}
for _st in ("run_shell", "browse_page", "read_file", "write_file", "append_file"):
    _cron_skill_handlers[_st] = _make_cron_skill_call_handler(_st)

_base._cron_skill_handlers = _cron_skill_handlers


def main() -> None:
    run_main(globals())


if __name__ == "__main__":
    main()
