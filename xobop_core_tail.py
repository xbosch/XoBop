from xobop_core_base import *
from xobop_core_base import _COMMAND_DISPATCH, _get_command_aliases
from telegram import Update
from telegram.ext import Application, ContextTypes

async def _artifact_watch_loop() -> None:
    global _seen_artifacts
    art = Path(ARTIFACT_DIR)
    art.mkdir(parents=True, exist_ok=True)
    _seen_artifacts = {str(p) for p in art.rglob("*") if p.is_file()}
    while True:
        try:
            current = {str(p) for p in art.rglob("*") if p.is_file()}
            new_files = sorted(current - _seen_artifacts)
            _seen_artifacts = current
            if new_files and _app is not None:
                newest = Path(new_files[-1])
                summary = await _quicklook_summary(newest)
                await _notify_owner(f"🆕 New artifact detected:\n{newest}\nQuick Look: {summary}")
                try:
                    ext = newest.suffix.lower()
                    if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
                        await _app.bot.send_photo(chat_id=OWNER_ID, photo=str(newest), caption="Artifact preview")
                    elif ext in {'.pdf', '.log', '.txt'}:
                        await _app.bot.send_document(chat_id=OWNER_ID, document=str(newest), caption="Artifact file")
                except Exception:
                    pass

                # Overseer-only multimodal indexing into memory.
                if _overseer_enabled() and newest.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.pdf'}:
                    try:
                        desc = await _multimodal_artifact_summary(newest)
                        mem = f"Artifact summary ({newest.name}): {desc} [path={newest}]"
                        await _add_memory(mem, mem_type="artifact_summary", importance=0.78, source="artifact_watcher", pinned=False)
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("Artifact watcher failed: %s", exc)
        await asyncio.sleep(30)


async def _notification_flush_loop() -> None:
    while True:
        try:
            if not _in_quiet_hours() and _app is not None:
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT id,chat_id,text FROM notification_queue WHERE delivered=0 ORDER BY id ASC LIMIT 5"
                    ).fetchall()
                for r in rows:
                    try:
                        await _app.bot.send_message(chat_id=int(r["chat_id"]), text=r["text"])
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE notification_queue SET delivered=1,delivered_ts=CURRENT_TIMESTAMP WHERE id=?",
                                (int(r["id"]),),
                            )
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("Notification flush failed: %s", exc)
        await asyncio.sleep(30)


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
    mode = _get_mode()

    def _set_last_latency(ms: float) -> None:
        global _last_openai_latency_ms
        _last_openai_latency_ms = ms

    token = _AGENT_TURN_ID.set(f"{chat_id}-{uuid.uuid4().hex[:12]}")
    try:
        deps = AgentDeps(
            get_history=_get_history,
            build_memory_block=_build_memory_block,
            build_tools_from_db=_build_tools_from_db,
            user_forced=_user_forced,
            get_runtime_prompt=_get_runtime_prompt,
            save_message=_save_message,
            maybe_promote_memory=_maybe_promote_memory,
            call_skill=_call_skill,
            redact=_redact,
            normalize_machine_kv_output=normalize_machine_kv_output,
            fire_and_forget=_fire_and_forget,
            set_last_openai_latency_ms=_set_last_latency,
            temperature_for_mode=_temperature_for_mode,
        )
        final_text = await run_agent_loop(
            chat_id=chat_id,
            query=query,
            force=force,
            mode=mode,
            client=client,
            model=MODEL,
            history_load_limit=HISTORY_LOAD_LIMIT,
            max_memory_results=MAX_MEMORY_RESULTS,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            history_max=MAX_HISTORY,
            history_trim_to=TRIM_TO,
            deps=deps,
            log=log,
        )
        return final_text
    finally:
        _AGENT_TURN_ID.reset(token)


# =============================================================================
# Status and metrics
# =============================================================================


def _system_pulse() -> dict[str, str]:
    pulse: dict[str, str] = {}
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        free_pct = (free / total * 100.0) if total > 0 else 0.0
        pulse["disk_free_pct"] = f"{free_pct:.1f}%"
        pulse["disk_status"] = "ok" if free_pct >= 10.0 else "low"
    except Exception:
        pulse["disk_free_pct"] = "n/a"
        pulse["disk_status"] = "unknown"

    try:
        load1, load5, _ = os.getloadavg()
        cpu_count = max(1, (os.cpu_count() or 1))
        load_norm = load1 / cpu_count
        pulse["load1"] = f"{load1:.2f}"
        pulse["load5"] = f"{load5:.2f}"
        pulse["load_status"] = "ok" if load_norm < 1.0 else "high"
    except Exception:
        pulse["load1"] = "n/a"
        pulse["load5"] = "n/a"
        pulse["load_status"] = "unknown"

    try:
        mem_total_kb = 0
        mem_avail_kb = 0
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
        mem_avail_pct = (mem_avail_kb / mem_total_kb * 100.0) if mem_total_kb > 0 else 0.0
        pulse["mem_avail_pct"] = f"{mem_avail_pct:.1f}%"
        pulse["mem_status"] = "ok" if mem_avail_pct >= 10.0 else "low"
    except Exception:
        pulse["mem_avail_pct"] = "n/a"
        pulse["mem_status"] = "unknown"

    # Optional thermal read (common Linux paths)
    temp_c = None
    for p in [
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
    ]:
        try:
            if p.exists():
                raw = p.read_text().strip()
                v = float(raw)
                temp_c = (v / 1000.0) if v > 1000 else v
                break
        except Exception:
            continue
    if temp_c is None:
        pulse["cpu_temp_c"] = "n/a"
        pulse["temp_status"] = "unknown"
    else:
        pulse["cpu_temp_c"] = f"{temp_c:.1f}"
        pulse["temp_status"] = "ok" if temp_c < 80.0 else "hot"

    return pulse


def _health_report() -> str:
    with get_db() as conn:
        db_size = Path(DB_FILE).resolve().stat().st_size if Path(DB_FILE).exists() else 0
        messages = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        memories = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"]
        skills = conn.execute("SELECT COUNT(*) AS n FROM skills WHERE enabled=1").fetchone()["n"]
        cron = conn.execute("SELECT COUNT(*) AS n FROM cron_jobs WHERE enabled=1").fetchone()["n"]
        open_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
        closed_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='done'").fetchone()["n"]
        errors = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status!='ok' AND timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["n"]
        avg_ms = conn.execute(
            "SELECT COALESCE(AVG(duration_ms),0) AS v FROM runs WHERE timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["v"]
        last_cron = conn.execute(
            "SELECT job_name,status,duration_ms FROM cron_runs ORDER BY id DESC LIMIT 3"
        ).fetchall()
    cron_tail = "\n".join([f"  - {r['job_name']}: {'✅' if r['status']=='ok' else '❌'} ({r['duration_ms']}ms)" for r in last_cron]) or "  - none"
    pulse = _system_pulse()
    return (
        f"• DB size: {db_size/1024/1024:.2f} MB\n"
        f"• Messages: {messages}\n"
        f"• Memories: {memories}\n"
        f"• Enabled skills: {skills}\n"
        f"• Active cron jobs: {cron}\n"
        f"• Tasks: open={open_tasks} closed={closed_tasks}\n"
        f"• OpenAI latency (last): {_last_openai_latency_ms:.1f} ms\n"
        f"• Avg tool latency 1h: {float(avg_ms):.1f} ms\n"
        f"• Errors 1h: {int(errors)}\n"
        f"• Pulse: disk={pulse.get('disk_free_pct','n/a')} ({pulse.get('disk_status','unknown')}), "
        f"mem={pulse.get('mem_avail_pct','n/a')} ({pulse.get('mem_status','unknown')}), "
        f"load1={pulse.get('load1','n/a')} ({pulse.get('load_status','unknown')}), "
        f"temp={pulse.get('cpu_temp_c','n/a')}C ({pulse.get('temp_status','unknown')})\n"
        f"• Last cron runs:\n{cron_tail}"
    )


def _diagnose_report(days: int = 7) -> str:
    return ig_diagnose_report(days=days, get_db=get_db)
def _generate_improvement_proposal(days: int = 7) -> tuple[int, str]:
    return ig_generate_improvement_proposal(days=days, get_db=get_db, diagnose_report_fn=_diagnose_report)
def _latest_artifact_for_proposal(proposal_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    return row


def _compile_proposal_to_change(proposal_id: int) -> str:
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
    )
def _sim_shell_policy_check(command: str) -> dict:
    hard_block = [
        r"\brm\s+-rf\s+/$",
        r"\brm\s+-rf\s+/\s",
        r"\bmkfs(\.|\s)",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bpoweroff\b",
    ]
    for pat in hard_block:
        if re.search(pat, command, re.I):
            return {"ok": False, "reason": f"hard-block:{pat}"}
    pol = _classify_shell(command)
    if pol.kind == "destructive":
        return {"ok": False, "reason": "destructive"}
    return {"ok": True}


def _build_shell_replay_diff(*, proposal_id: int, artifact: dict, replay_results: list[dict], golden_results: list[dict], manifest) -> dict:
    targeted = [r for r in replay_results if r.get("case_class") == "targeted"]
    targeted_improved = sum(1 for r in targeted if bool(r.get("actual_ok")) == bool(r.get("expect_ok", True)))
    targeted_regressed = sum(1 for r in targeted if bool(r.get("actual_ok")) != bool(r.get("expect_ok", True)))
    golden_failed = sum(1 for r in golden_results if not r.get("pass"))
    policy_violations = 0
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
            got = classify(text)["name"]
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
def _capture_evolution_memory_sync(proposal_id: int) -> str:
    return ipo_capture_evolution_memory_sync(proposal_id=proposal_id, get_db=get_db)
def _evolution_memory_report(limit: int = 10) -> str:
    return ipo_evolution_memory_report(limit=limit, get_db=get_db)
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
    return update.effective_chat.id == OWNER_ID


async def _reply(update: Update, text: str) -> None:
    # Persist all command-path assistant replies for observability/tests.
    try:
        chat_id = update.effective_chat.id if update and update.effective_chat else 0
        if chat_id:
            loop = asyncio.get_running_loop()
            _fire_and_forget(loop, lambda: _save_message(chat_id, "assistant", text), label="save_reply_msg")
    except Exception:
        pass

    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except TelegramError:
        try:
            await update.message.reply_text(text)
        except TelegramError as exc:
            log.error("Failed to send reply: %s", exc)


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
    import subprocess
    checks = []
    code_path = Path(__file__).resolve()
    # Pin to project venv explicitly (based on XoBop.py location) to avoid cwd/interpreter drift.
    # IMPORTANT: do not .resolve() here; many venv python binaries are symlinks to /usr/bin/python,
    # and resolving would bypass venv context.
    venv_python = (code_path.parent / '.venv' / 'bin' / 'python')
    checks.append(("venv_python", venv_python.exists()))
    req = Path('requirements.txt').resolve()
    checks.append(("requirements_txt", req.exists()))
    for key in ["TELEGRAM_TOKEN", "OPENAI_API_KEY", "OWNER_ID", "DB_FILE"]:
        checks.append((f"env_{key}", bool(os.getenv(key))))
    # lightweight dependency check
    dep_ok = True
    dep_note = "ok"
    if venv_python.exists() and req.exists():
        try:
            proc = subprocess.run(
                [str(venv_python), "-m", "pip", "check"],
                text=True,
                capture_output=True,
                timeout=20,
            )
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                dep_ok = True
                dep_note = (out or "No broken requirements found")[:180]
            else:
                dep_ok = False
                dep_note = (out or f"pip check failed (exit {proc.returncode})")[:180]
        except Exception as exc:
            dep_ok = False
            dep_note = str(exc)[:180]
    checks.append(("pip_check", dep_ok))

    pulse = _system_pulse()
    checks.append(("pulse_disk", pulse.get("disk_status") in {"ok", "unknown"}))
    checks.append(("pulse_mem", pulse.get("mem_status") in {"ok", "unknown"}))
    checks.append(("pulse_load", pulse.get("load_status") in {"ok", "unknown"}))
    checks.append(("pulse_temp", pulse.get("temp_status") in {"ok", "unknown"}))

    bad = [name for name, ok in checks if not ok]
    git_rev = "unknown"
    try:
        git_rev = subprocess.check_output(["git", "-C", str(code_path.parent), "rev-parse", "--short", "HEAD"], text=True).strip() or "unknown"
    except Exception:
        pass

    lines = [f"• {name}: {'✅' if ok else '❌'}" for name, ok in checks]
    lines.append(f"• code_path: {code_path}")
    lines.append(f"• git_rev: {git_rev}")
    lines.append(f"• venv_python_path: {venv_python}")
    lines.append(f"• venv_python_resolved: {venv_python.resolve()}")
    lines.append(
        f"• pulse: disk={pulse.get('disk_free_pct','n/a')} ({pulse.get('disk_status','unknown')}), "
        f"mem={pulse.get('mem_avail_pct','n/a')} ({pulse.get('mem_status','unknown')}), "
        f"load1={pulse.get('load1','n/a')} ({pulse.get('load_status','unknown')}), "
        f"temp={pulse.get('cpu_temp_c','n/a')}C ({pulse.get('temp_status','unknown')})"
    )
    lines.append(f"• pip_check_note: {dep_note}")
    if bad:
        lines.append(f"RESULT: FAIL ({', '.join(bad)})")
    else:
        lines.append("RESULT: PASS")
    return "\n".join(lines)


def _generate_system_map_sync() -> str:
    root = Path(os.getcwd())
    out = root / 'memory' / 'SYSTEM_MAP.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted([str(p.relative_to(root)) for p in (root / 'xobop').glob('*.py')])
    body = [
        "# SYSTEM_MAP.md",
        "",
        "Auto-generated architecture snapshot.",
        "",
        "## Modules",
        *[f"- `{f}`" for f in files],
        "",
        "## Entry",
        "- `XoBop.py`",
    ]
    out.write_text("\n".join(body), encoding='utf-8')
    return str(out)


async def cmd_checkup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    loop = asyncio.get_running_loop()
    rep = await loop.run_in_executor(None, _run_checkup_sync)
    _record_op_event(
        "health_check",
        chat_id=update.effective_chat.id,
        route="cmd_checkup",
        status=("ok" if "RESULT: PASS" in rep else "error"),
    )
    await _reply(update, rep)


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    goal = update.message.text.partition(" ")[2].strip()
    if not goal:
        await _reply(update, "Use: `/plan <goal>`")
        return
    # deterministic simple manifest scaffold
    steps = [
        "1) Discover relevant files/resources",
        "2) Run analysis/execution command",
        "3) Validate output",
        "4) Report concise result",
    ]
    STATE.planned_manifest_by_chat[chat_id] = {"goal": goal, "steps": steps}
    await _reply(update, "Planned manifest:\n" + "\n".join(steps) + "\nReply `/go` to execute.")


async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        return
    chat_id = update.effective_chat.id
    plan = STATE.planned_manifest_by_chat.get(chat_id)
    if not plan:
        await _reply(update, "No pending plan. Use `/plan <goal>` first.")
        return
    goal = plan.get("goal", "")
    # execute through agent with explicit imperative
    result = await _run_agent(chat_id, f"Execute this plan now: {goal}", force=True)
    STATE.planned_manifest_by_chat.pop(chat_id, None)
    await _reply(update, result)


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


async def cmd_op_sanity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_op_sanity_impl(update=update, owner_only=_owner_only, reply=_reply, op_sanity_report=_op_sanity_report)


async def cmd_diagnose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_diagnose_impl(update=update, owner_only=_owner_only, reply=_reply, diagnose_report=_diagnose_report)


async def cmd_improve_propose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_improve_propose_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        generate_improvement_proposal=_generate_improvement_proposal,
        record_op_event=_record_op_event,
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


async def cmd_evolve_capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_capture_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        to_thread=_to_thread,
        capture_evolution_memory_sync=_capture_evolution_memory_sync,
    )


async def cmd_evolve_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_evolve_memory_impl(
        update=update,
        owner_only=_owner_only,
        reply=_reply,
        evolution_memory_report=_evolution_memory_report,
    )


async def cmd_topskills(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_topskills_impl(update=update, owner_only=_owner_only, reply=_reply, top_skills=_top_skills)


async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memories_impl(update=update, owner_only=_owner_only, reply=_reply, list_memories=_list_memories)


async def cmd_memadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memadd_impl(update=update, owner_only=_owner_only, reply=_reply, add_memory=_add_memory)


async def cmd_memdel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_memdel_impl(update=update, owner_only=_owner_only, reply=_reply, delete_memory_sync=_delete_memory_sync)


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
        panic_unlock_code=PANIC_UNLOCK_CODE,
        set_setting=_set_setting,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help_impl(update=update, owner_only=_owner_only, reply=_reply)


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    dispatch = {k: (lambda u, fn=fn: fn(u, ctx)) for k, fn in _COMMAND_DISPATCH.items()}
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
        process_media_message_fn=lambda u: process_media_message(
            update=u,
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

    async def _process_text(u, t, c):
        await process_text_message(
            update=u,
            text=t,
            chat_id=c,
            state=STATE,
            reply=_reply,
            call_skill=_call_skill,
            mode_getter=_get_mode,
            list_cron_jobs=_list_cron_jobs,
            run_agent=_run_agent,
            owner_only=_owner_only,
            cmd_cronadd=lambda u2: cmd_cronadd(u2, ctx),
            cmd_cronlist=lambda u2: cmd_cronlist(u2, ctx),
            cmd_cronrun=lambda u2: cmd_cronrun(u2, ctx),
            cmd_cronruns=lambda u2: cmd_cronruns(u2, ctx),
            cmd_status=lambda u2: cmd_status(u2, ctx),
            cmd_health=lambda u2: cmd_health(u2, ctx),
            cmd_checkup=lambda u2: cmd_checkup(u2, ctx),
            cmd_state_check=lambda u2: cmd_state_check(u2, ctx),
            cmd_state_repair=lambda u2: cmd_state_repair(u2, ctx),
            cmd_op_sanity=lambda u2: cmd_op_sanity(u2, ctx),
            cmd_diagnose=lambda u2: cmd_diagnose(u2, ctx),
            cmd_improve_propose=lambda u2: cmd_improve_propose(u2, ctx),
            cmd_improve_list=lambda u2: cmd_improve_list(u2, ctx),
            cmd_improve_compile=lambda u2: cmd_improve_compile(u2, ctx),
            cmd_improve_artifacts=lambda u2: cmd_improve_artifacts(u2, ctx),
            cmd_improve_simulate=lambda u2: cmd_improve_simulate(u2, ctx),
            cmd_improve_runs=lambda u2: cmd_improve_runs(u2, ctx),
            cmd_improve_compare=lambda u2: cmd_improve_compare(u2, ctx),
            cmd_improve_policy=lambda u2: cmd_improve_policy(u2, ctx),
            cmd_improve_promote=lambda u2: cmd_improve_promote(u2, ctx),
            cmd_improve_autopromote=lambda u2: cmd_improve_autopromote(u2, ctx),
            cmd_improve_funnel=lambda u2: cmd_improve_funnel(u2, ctx),
            cmd_improve_eval_report=lambda u2: cmd_improve_eval_report(u2, ctx),
            cmd_improve_weekly_report=lambda u2: cmd_improve_weekly_report(u2, ctx),
            cmd_improve_phase=lambda u2: cmd_improve_phase(u2, ctx),
            cmd_improve_log_rollback=lambda u2: cmd_improve_log_rollback(u2, ctx),
            cmd_evolve_capture=lambda u2: cmd_evolve_capture(u2, ctx),
            cmd_evolve_memory=lambda u2: cmd_evolve_memory(u2, ctx),
            cmd_panicstatus=lambda u2: cmd_panicstatus(u2, ctx),
            cmd_paniclock=lambda u2: cmd_paniclock(u2, ctx),
            cmd_panicunlock=lambda u2: cmd_panicunlock(u2, ctx),
            panic_locked=_panic_locked,
            panic_bypass_text=_is_panic_bypass_text,
            command_aliases_getter=_get_command_aliases,
            analyze_attachment=_analyze_attachment,
            op_event=_record_op_event,
            log=log,
        )

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
    global _app, _state_task, _artifact_task, _notify_task
    _app = app

    def _set_tasks(t1, t2, t3):
        global _state_task, _artifact_task, _notify_task
        _state_task, _artifact_task, _notify_task = t1, t2, t3

    await al_post_init_impl(
        app=app,
        build_scheduler=_build_scheduler,
        sync_scheduler=_sync_scheduler,
        log=log,
        load_session_state=_load_session_state,
        state=STATE,
        notify_owner=_notify_owner,
        state_persist_loop=_state_persist_loop,
        artifact_watch_loop=_artifact_watch_loop,
        notification_flush_loop=_notification_flush_loop,
        set_tasks=_set_tasks,
    )


async def _post_shutdown(app: Application) -> None:
    global _state_task, _artifact_task, _notify_task

    def _clear_tasks():
        global _state_task, _artifact_task, _notify_task
        _state_task = None
        _artifact_task = None
        _notify_task = None

    await al_post_shutdown_impl(
        app=app,
        log=log,
        tasks=(_state_task, _artifact_task, _notify_task),
        clear_tasks=_clear_tasks,
    )


async def _graceful_shutdown(app: Application) -> None:
    await al_graceful_shutdown_impl(app=app, log=log)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    await al_on_error_impl(update=update, context=context, log=log)


def _apply_self_mods_to_runtime() -> None:
    sm_apply_defaults(get_setting=_get_setting, set_setting=_set_setting)
    _export_self_mods_debug()


def _startup_reconcile_runtime_state() -> None:
    return rsnap_startup_reconcile(
        load_snapshot=_load_runtime_snapshot,
        apply_self_mods_to_runtime=_apply_self_mods_to_runtime,
        export_runtime_snapshot_fn=_export_runtime_snapshot,
        get_last_applied_artifact_id=_get_last_applied_artifact_id,
        state_check_dict=_state_check_dict,
        repair_state_from_snapshot=_repair_state_from_snapshot,
        set_setting=_set_setting,
        json_dumps=json.dumps,
    )


def main() -> None:
    _init_db()
    _startup_reconcile_runtime_state()
    if _get_setting("mode") not in MODES:
        _set_setting("mode", DEFAULT_MODE if DEFAULT_MODE in MODES else "strict")

    log.info(
        "XaviExecutor V5 | Owner: %d | Model: %s | Embeddings: %s | Mode: %s | Write root: %s",
        OWNER_ID,
        MODEL,
        EMBEDDING_MODEL,
        _get_mode(),
        ALLOWED_WRITE_ROOT,
    )

    app = build_app(token=TOKEN, post_init=_post_init, post_shutdown=_post_shutdown)
    commands = {
            "remember": cmd_remember,
            "delfact": cmd_delfact,
            "facts": cmd_facts,
            "memories": cmd_memories,
            "memadd": cmd_memadd,
            "memdel": cmd_memdel,
            "addtask": cmd_addtask,
            "donetask": cmd_donetask,
            "tasks": cmd_tasks,
            "mode": cmd_mode,
            "promptshow": cmd_promptshow,
            "promptset": cmd_promptset,
            "promptproposal": cmd_promptproposal,
            "promptapprove": cmd_promptapprove,
            "promptreject": cmd_promptreject,
            "quiethours": cmd_quiethours,
            "quietoff": cmd_quietoff,
            "quietflush": cmd_quietflush,
            "status": cmd_status,
            "health": cmd_health,
            "checkup": cmd_checkup,
            "state_check": cmd_state_check,
            "state_repair": cmd_state_repair,
            "op_sanity": cmd_op_sanity,
            "diagnose": cmd_diagnose,
            "improve_propose": cmd_improve_propose,
            "improve_list": cmd_improve_list,
            "improve_compile": cmd_improve_compile,
            "improve_artifacts": cmd_improve_artifacts,
            "improve_simulate": cmd_improve_simulate,
            "improve_runs": cmd_improve_runs,
            "improve_compare": cmd_improve_compare,
            "improve_policy": cmd_improve_policy,
            "improve_promote": cmd_improve_promote,
            "improve_autopromote": cmd_improve_autopromote,
            "improve_funnel": cmd_improve_funnel,
            "improve_eval_report": cmd_improve_eval_report,
            "improve_weekly_report": cmd_improve_weekly_report,
            "improve_phase": cmd_improve_phase,
            "improve_log_rollback": cmd_improve_log_rollback,
            "evolve_capture": cmd_evolve_capture,
            "evolve_memory": cmd_evolve_memory,
            "plan": cmd_plan,
            "go": cmd_go,
            "topskills": cmd_topskills,
            "exec": cmd_exec,
            "attachdiag": cmd_attachdiag,
            "panicstatus": cmd_panicstatus,
            "paniclock": cmd_paniclock,
            "panicunlock": cmd_panicunlock,
            "skills": cmd_skills,
            "skillon": cmd_skillon,
            "skilloff": cmd_skilloff,
            "clearctx": cmd_clearctx,
            "help": cmd_help,
            "cronadd": cmd_cronadd,
            "cronrm": cmd_cronrm,
            "cronlist": cmd_cronlist,
            "cronon": cmd_cronon,
            "cronoff": cmd_cronoff,
            "cronrun": cmd_cronrun,
            "cronruns": cmd_cronruns,
    }

    global _COMMAND_DISPATCH
    _COMMAND_DISPATCH = dict(commands)

    register_handlers(
        app=app,
        commands=commands,
        cmd_unknown=cmd_unknown,
        handle_media_message=handle_media_message,
        handle_message=handle_message,
        on_error=_on_error,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_graceful_shutdown(app)))

    app.run_polling(drop_pending_updates=True, allowed_updates=["message"], close_loop=False)


if __name__ == "__main__":
    main()
