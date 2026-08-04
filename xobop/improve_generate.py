from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Callable

TESTER_NOISE_COMMANDS: frozenset[str] = frozenset({"/unknowncommand"})


def build_improve_priorities_block(priorities_file: Path) -> str:
    """Read the human-edited steering doc for /improve_propose, if present.

    Mirrors build_file_memory_block_impl's shape (xobop/core_memory_runtime.py)
    so proposal generation can be steered the same way chat replies are
    steered by MEMORY.md, without inventing a new file-reading convention.
    """
    if not priorities_file.exists():
        return ""
    txt = priorities_file.read_text(encoding="utf-8", errors="ignore").strip()
    return txt[:2000] if txt else ""


def _backfill_op_signals_daily(*, get_db: Callable, days: int) -> None:
    d = max(1, min(days, 30))
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT date(timestamp) AS day, event_type, status, retries, latency_ms, command
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
            ORDER BY timestamp ASC
            """,
            (f"-{d} day",),
        ).fetchall()

        if not rows:
            return

        by_day = defaultdict(lambda: {"events": 0, "fails": 0, "unknown": 0, "retries": 0, "lat": []})
        for r in rows:
            day = str(r["day"] or "")
            if not day:
                continue
            b = by_day[day]
            b["events"] += 1
            status = str(r["status"] or "ok")
            if status not in {"ok", "skipped"}:
                b["fails"] += 1
            if str(r["event_type"] or "") == "unknown_command" and str(r["command"] or "") not in TESTER_NOISE_COMMANDS:
                b["unknown"] += 1
            b["retries"] += int(r["retries"] or 0)
            lat = r["latency_ms"]
            if lat is not None:
                try:
                    b["lat"].append(float(lat))
                except Exception:
                    pass

        for day, b in by_day.items():
            lat = sorted(b["lat"])
            med = float(median(lat)) if lat else 0.0
            if lat:
                idx = max(0, min(len(lat) - 1, int(round(0.95 * (len(lat) - 1)))))
                p95 = float(lat[idx])
            else:
                p95 = 0.0
            conn.execute(
                """
                INSERT INTO op_signals_daily(day, events_total, failures, command_unknown, retries_total, median_latency_ms, p95_latency_ms, updated_ts)
                VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(day) DO UPDATE SET
                    events_total=excluded.events_total,
                    failures=excluded.failures,
                    command_unknown=excluded.command_unknown,
                    retries_total=excluded.retries_total,
                    median_latency_ms=excluded.median_latency_ms,
                    p95_latency_ms=excluded.p95_latency_ms,
                    updated_ts=CURRENT_TIMESTAMP
                """,
                (day, b["events"], b["fails"], b["unknown"], b["retries"], med, p95),
            )


def diagnose_report(*, days: int, get_db: Callable) -> str:
    d = max(1, min(days, 30))
    _backfill_op_signals_daily(get_db=get_db, days=d)
    with get_db() as conn:
        daily = conn.execute(
            """
            SELECT day,events_total,failures,command_unknown,retries_total,median_latency_ms,p95_latency_ms
            FROM op_signals_daily
            WHERE day >= date('now', ?)
            ORDER BY day DESC
            LIMIT 7
            """,
            (f"-{d} day",),
        ).fetchall()
        fail_routes = conn.execute(
            """
            WITH totals AS (
                SELECT COALESCE(route,'') AS route, COUNT(*) AS total
                FROM op_events
                WHERE timestamp >= datetime('now', ?)
                GROUP BY COALESCE(route,'')
            ),
            fails AS (
                SELECT COALESCE(route,'') AS route, COUNT(*) AS n
                FROM op_events
                WHERE timestamp >= datetime('now', ?)
                  AND status NOT IN ('ok','skipped')
                GROUP BY COALESCE(route,'')
            )
            SELECT f.route, f.n, t.total,
                   ROUND(100.0 * f.n / t.total, 1) AS error_rate
            FROM fails f
            JOIN totals t ON t.route = f.route
            ORDER BY error_rate DESC
            LIMIT 5
            """,
            (f"-{d} day", f"-{d} day"),
        ).fetchall()
        _noise_ph = ",".join("?" * len(TESTER_NOISE_COMMANDS))
        unknown_cmds = conn.execute(
            f"""
            SELECT COALESCE(command,'') AS cmd, COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND event_type='unknown_command'
              AND COALESCE(command,'') NOT IN ({_noise_ph})
            GROUP BY COALESCE(command,'')
            ORDER BY n DESC
            LIMIT 5
            """,
            (f"-{d} day", *TESTER_NOISE_COMMANDS),
        ).fetchall()
        since = conn.execute("SELECT MIN(timestamp) AS ts FROM op_events").fetchone()["ts"]

    if not daily and not fail_routes and not unknown_cmds:
        return "No diagnosis data yet. Run workload and `scripts/build_signals.py` first."

    total_events = sum(int(r["events_total"] or 0) for r in daily)
    total_fail = sum(int(r["failures"] or 0) for r in daily)
    total_unknown = sum(int(r["command_unknown"] or 0) for r in daily)
    fail_rate = (100.0 * total_fail / total_events) if total_events else 0.0

    lines = [
        f"Diagnosis window: last {d} day(s)",
        f"Data since: {since or 'n/a'}",
        f"Events: {total_events} | Failures: {total_fail} ({fail_rate:.1f}%) | Unknown commands: {total_unknown}",
    ]
    if daily:
        lines.append("Daily summary:")
        for r in daily[:5]:
            lines.append(f"- {r['day']}: events={int(r['events_total'] or 0)} fail={int(r['failures'] or 0)} unk={int(r['command_unknown'] or 0)} p95={float(r['p95_latency_ms'] or 0):.0f}ms")
    if fail_routes:
        lines.append("Top failing routes (by error rate):")
        for r in fail_routes:
            lines.append(f"- {(r['route'] or '(none)')}: {int(r['n'])} failures / {int(r['total'])} total ({float(r['error_rate'] or 0):.1f}% error rate)")
    if unknown_cmds:
        lines.append("Top unknown commands:")
        for r in unknown_cmds:
            lines.append(f"- {(r['cmd'] or '(empty)')}: {int(r['n'])}")

    likely = []
    _unknown_rate = (total_unknown / total_events) if total_events else 0.0
    if total_unknown >= 5 and _unknown_rate > 0.02:
        likely.append("Command discoverability issue: high unknown-command volume")
    if any((r["route"] or "") == "run_shell" for r in fail_routes):
        likely.append("Shell route instability: investigate policy blocks/path assumptions")
    if daily and any(float(r["p95_latency_ms"] or 0) > 4000 for r in daily):
        likely.append("Latency pressure: p95 over 4s in diagnosis window")
    if likely:
        lines.append("Likely causes:")
        lines.extend([f"- {x}" for x in likely])
    return "\n".join(lines)


def diagnose_snapshot(*, days: int, get_db: Callable) -> dict[str, Any]:
    """Return a structured diagnosis dict for use in the compile path.
    Includes per-route blocked_count and error_count so compile can discriminate
    policy blocks from real errors without re-querying the DB."""
    d = max(1, min(days, 30))
    with get_db() as conn:
        fail_routes = conn.execute(
            """
            WITH totals AS (
                SELECT COALESCE(route,'') AS route, COUNT(*) AS total
                FROM op_events WHERE timestamp >= datetime('now', ?)
                GROUP BY COALESCE(route,'')
            ),
            fails AS (
                SELECT COALESCE(route,'') AS route, COUNT(*) AS n
                FROM op_events
                WHERE timestamp >= datetime('now', ?) AND status NOT IN ('ok','skipped')
                GROUP BY COALESCE(route,'')
            )
            SELECT f.route, f.n AS error_total, t.total,
                   ROUND(100.0 * f.n / t.total, 1) AS error_rate
            FROM fails f JOIN totals t ON t.route = f.route
            ORDER BY error_rate DESC LIMIT 10
            """,
            (f"-{d} day", f"-{d} day"),
        ).fetchall()
        # Per-route blocked vs error breakdown
        top_failing_routes = []
        for r in fail_routes:
            route = str(r["route"] or "")
            blocked = int(conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE route=? AND status='blocked' AND timestamp >= datetime('now', ?)",
                (route, f"-{d} day"),
            ).fetchone()["n"] or 0)
            errored = int(conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE route=? AND status NOT IN ('ok','skipped','blocked') AND timestamp >= datetime('now', ?)",
                (route, f"-{d} day"),
            ).fetchone()["n"] or 0)
            top_failing_routes.append({
                "route": route,
                "total": int(r["total"] or 0),
                "error_total": int(r["error_total"] or 0),
                "blocked_count": blocked,
                "error_count": errored,
                "error_rate": float(r["error_rate"] or 0),
            })
        _noise_ph = ",".join("?" * len(TESTER_NOISE_COMMANDS))
        total_unknown = conn.execute(
            f"SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) AND event_type='unknown_command' AND COALESCE(command,'') NOT IN ({_noise_ph})",
            (f"-{d} day", *TESTER_NOISE_COMMANDS),
        ).fetchone()["n"]
        agg = conn.execute(
            "SELECT COALESCE(SUM(events_total),0) AS ev, COALESCE(SUM(failures),0) AS fl FROM op_signals_daily WHERE day >= date('now', ?)",
            (f"-{d} day",),
        ).fetchone()
    total_events = int(agg["ev"] or 0)
    total_fail = int(agg["fl"] or 0)
    return {
        "days": d,
        "total_events": total_events,
        "total_fail": total_fail,
        "fail_rate": (100.0 * total_fail / total_events) if total_events else 0.0,
        "total_unknown": int(total_unknown or 0),
        "top_failing_routes": top_failing_routes,
    }


def llm_artifact_proposal_from_diagnosis(*, diagnosis_report: str, allowed_families: list[str], llm_complete: Callable[[str], str], prior_context: str = "", priorities_text: str = "") -> dict[str, Any] | None:
    """Opt-in LLM proposal path.

    Expects model to return JSON object with keys:
    title, problem, cause, change, benefit, metric, risk, artifact_family
    """
    allowed = [x.strip().lower() for x in allowed_families if str(x).strip()]
    prompt = (
        "You are proposing one safe improvement artifact for a production assistant. "
        "Use only these artifact families: " + ", ".join(allowed) + ". "
        "Return strict JSON with keys: title, problem, cause, change, benefit, metric, risk, artifact_family. "
        "risk must be one of: low, medium, high. Keep concise and concrete.\n\n"
        + (f"Current priorities (from human-edited steering doc):\n{priorities_text}\n\n" if priorities_text else "")
        + (f"Prior outcomes for similar failures:\n{prior_context}\n\n" if prior_context else "")
        + "Diagnosis report:\n" + diagnosis_report
    )
    try:
        raw = llm_complete(prompt)
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        fam = str(obj.get("artifact_family", "")).strip().lower()
        if fam and fam not in allowed:
            return None
        risk = str(obj.get("risk", "low")).strip().lower()
        if risk not in {"low", "medium", "high"}:
            obj["risk"] = "low"
        return obj
    except Exception:
        return None


def _diagnosis_contains(diagnosis_text: str, *terms: str) -> bool:
    t = (diagnosis_text or "").lower()
    return any(x.lower() in t for x in terms)


def generate_improvement_proposal(*, days: int, get_db: Callable, diagnose_report_fn: Callable[[int], str], llm_proposal_fn: Callable[[str], str] | None = None, allowed_families_fn: Callable[[], list[str]] | None = None, lookup_evolution_fn: Callable[[str, str], list[dict]] | None = None, conditioned_evolution_rows: list[dict] | None = None, priorities_text: str = "") -> tuple[int, str]:
    d = max(1, min(days, 30))
    diagnosis_text = diagnose_report_fn(d)
    risk = "low"
    title = "Ops improvement proposal"
    problem = "General reliability friction detected in recent diagnostics."
    cause = "Mixed routing/latency/command UX friction."
    change = "Tune command discoverability and route diagnostics."
    benefit = "Lower failure/unknown-command rates with stable latency."
    metric = "failures down; unknown-command count down; p95 latency stable"

    with get_db() as conn:
        fail_routes = conn.execute(
            """
            WITH totals AS (
                SELECT COALESCE(route,'') AS route, COUNT(*) AS total_calls
                FROM op_events
                WHERE timestamp >= datetime('now', ?)
                GROUP BY COALESCE(route,'')
            ),
            fails AS (
                SELECT COALESCE(route,'') AS route, COUNT(*) AS n
                FROM op_events
                WHERE timestamp >= datetime('now', ?)
                  AND status NOT IN ('ok','skipped')
                GROUP BY COALESCE(route,'')
            )
            SELECT fails.route, fails.n,
                   fails.n * 1.0 / COALESCE(totals.total_calls, 1) AS error_rate
            FROM fails
            JOIN totals ON fails.route = totals.route
            ORDER BY error_rate DESC
            LIMIT 1
            """,
            (f"-{d} day", f"-{d} day"),
        ).fetchone()
        _noise = tuple(TESTER_NOISE_COMMANDS)
        _noise_placeholders = ",".join("?" * len(_noise))
        unknown = conn.execute(
            f"SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) AND event_type='unknown_command' AND COALESCE(command,'') NOT IN ({_noise_placeholders})",
            (f"-{d} day", *_noise),
        ).fetchone()["n"]
        p95 = conn.execute("SELECT COALESCE(MAX(p95_latency_ms),0) AS p95 FROM op_signals_daily WHERE day >= date('now', ?)", (f"-{d} day",)).fetchone()["p95"]

        # Render conditioned evolution-memory priors (if the caller pre-fetched
        # them) so the LLM path actually sees past outcomes, not just the
        # heuristic fallback path.
        _llm_prior_context = ""
        if conditioned_evolution_rows:
            _lines = []
            for _p in conditioned_evolution_rows[:3]:
                _delta_str = f", Δfail_rate={_p['metric_delta']:+.2f}pp" if _p.get("metric_delta") is not None else ""
                _lines.append(f"- {_p.get('artifact_family','?')} on '{_p.get('target_route') or 'unknown'}' ({_p.get('outcome','?')}{_delta_str}): {_p.get('lesson','')}")
            _llm_prior_context = "\n".join(_lines)

        # Opt-in LLM path: if callback provided, let model propose a concrete artifact-facing plan.
        llm_used = False
        if llm_proposal_fn and allowed_families_fn:
            llm_obj = llm_artifact_proposal_from_diagnosis(
                diagnosis_report=diagnosis_text,
                allowed_families=allowed_families_fn(),
                llm_complete=llm_proposal_fn,
                prior_context=_llm_prior_context,
                priorities_text=priorities_text,
            )
            if llm_obj:
                llm_used = True
                risk = str(llm_obj.get("risk", risk) or risk).lower()
                title = str(llm_obj.get("title", title) or title)
                problem = str(llm_obj.get("problem", problem) or problem)
                cause = str(llm_obj.get("cause", cause) or cause)
                change = str(llm_obj.get("change", change) or change)
                benefit = str(llm_obj.get("benefit", benefit) or benefit)
                metric = str(llm_obj.get("metric", metric) or metric)

        diagnosis_unknown = _diagnosis_contains(diagnosis_text, "unknown command", "unknown commands")
        diagnosis_shell = _diagnosis_contains(diagnosis_text, "shell route instability", "run_shell")
        diagnosis_latency = _diagnosis_contains(diagnosis_text, "latency pressure", "p95 over 4s", "p95 latency")

        # Derive failure_class for structured evolution memory lookup and storage
        _target_route = str(fail_routes["route"] if fail_routes and fail_routes["route"] else "")
        if diagnosis_unknown:
            _failure_class = "unknown_command"
        elif fail_routes and (fail_routes["route"] or ""):
            _failure_class = "route_failure"
        elif float(p95 or 0) > 4000 or diagnosis_latency:
            _failure_class = "latency"
        else:
            _failure_class = "general"

        # Check evolution memory for prior successful responses to the same failure class.
        # conditioned_evolution_rows (pre-scored by diagnosis embedding) takes precedence.
        # Surfaced in the output note regardless of path (LLM or heuristic) so priors
        # are never silently discarded once the LLM path succeeds.
        _prior_note = ""
        if conditioned_evolution_rows is not None:
            _priors = conditioned_evolution_rows
        elif lookup_evolution_fn:
            _priors = lookup_evolution_fn(_failure_class, _target_route)
        else:
            _priors = []
        if _priors:
            _p = _priors[0]
            _delta_str = f", Δfail_rate={_p['metric_delta']:+.2f}pp" if _p.get("metric_delta") is not None else ""
            _prior_note = f" [Prior: {_p.get('artifact_family','?')} addressed {_failure_class} on '{_p.get('target_route') or 'unknown'}'{_delta_str}]"

        if diagnosis_unknown and not llm_used:
            title = "Reduce unknown command friction"
            problem = f"Unknown-command friction detected in diagnosis over last {d} day(s)."
            cause = "Command discoverability/help text mismatch with user inputs."
            change = "Add command alias hints and improve unknown-command suggestion mapping."
            benefit = "Fewer dead-end command attempts and lower correction loops."
            metric = "unknown_command count reduced by >=50% over next 7 days"
        elif ((fail_routes and (fail_routes["route"] or "")) or diagnosis_shell) and not llm_used:
            route = (fail_routes["route"] if fail_routes and (fail_routes["route"] or "") else "run_shell")
            risk = "medium" if route in {"run_shell", "cmd_exec"} else "low"
            title = f"Stabilize failing route: {route}"
            problem = f"Route '{route}' instability detected in diagnosis window."
            cause = "Route-specific policy/path/validation friction."
            change = f"Add pre-validation and clearer blocked/error responses for route '{route}'."
            benefit = "Lower route failure rate and fewer retries."
            metric = f"failures on route '{route}' reduced by >=40%"
        elif (float(p95 or 0) > 4000 or diagnosis_latency) and not llm_used:
            risk = "medium"
            title = "Reduce p95 latency pressure"
            problem = f"Latency pressure detected in diagnosis over last {d} day(s)."
            cause = "Tool/LLM path latency accumulation under current flow."
            change = "Add fast-path short-circuiting for deterministic intents and tighter timeout tiers."
            benefit = "Improved responsiveness without reducing correctness."
            metric = "p95 latency < 3000ms while failure rate does not increase"

        _ip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(improvement_proposals)").fetchall()}
        if "failure_class" in _ip_cols:
            cur = conn.execute(
                "INSERT INTO improvement_proposals (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target,failure_class) VALUES ('proposed',?,?,?,?,?,?,?,?)",
                (risk, title, problem, cause, change, benefit, metric, _failure_class),
            )
        else:
            cur = conn.execute(
                "INSERT INTO improvement_proposals (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target) VALUES ('proposed',?,?,?,?,?,?,?)",
                (risk, title, problem, cause, change, benefit, metric),
            )
        pid = int(cur.lastrowid)

    source_tag = "llm" if llm_used else "heuristic"
    out = f"Proposal #{pid} [{risk}] [{source_tag}]\nTitle: {title}\nProblem: {problem}\nCause: {cause}\nChange: {change}\nExpected: {benefit}\nMetric target: {metric}\nSource window: last {d} day(s)\nDiagnosis used: yes\nFailure class: {_failure_class}{_prior_note}"
    return pid, out


def compile_proposal_to_change(*, proposal_id: int, get_db: Callable, get_command_aliases: Callable, get_exec_retry_limit: Callable, get_last_applied_artifact_id: Callable, get_mode: Callable, get_runtime_prompt: Callable, artifact_contract: Callable, allowed_artifact_families: Callable, diagnosis_snapshot: dict | None = None) -> str:
    with get_db() as conn:
        _ip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(improvement_proposals)").fetchall()}
        _fc_sel = ",failure_class" if "failure_class" in _ip_cols else ""
        p = conn.execute(f"SELECT id,title,problem_statement,suspected_cause,proposed_change,metric_target{_fc_sel} FROM improvement_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")
        text = " ".join([str(p["title"] or ""), str(p["problem_statement"] or ""), str(p["suspected_cause"] or ""), str(p["proposed_change"] or ""), str(p["metric_target"] or "")]).lower()
        artifact_type = target = expected_metric = ""
        expected_delta = None
        artifact: dict[str, Any] = {}

        # Use stored failure_class as primary signal to select compile branch.
        # Keyword matching on text is fallback only — avoids semantic loss where a
        # route-specific retry proposal mis-compiles to a global retry artifact.
        _fc = str(p["failure_class"] if "failure_class" in _ip_cols else "").strip()

        def _is_route_instability() -> bool:
            if _fc == "route_failure":
                return True
            return bool("stabilize" in text or ("instability" in text and "route" in text) or "failing route" in text)

        def _is_unknown_command() -> bool:
            if _fc == "unknown_command":
                return True
            return bool("unknown command" in text or "alias" in text or "discoverability" in text)

        def _is_latency_or_retry() -> bool:
            if _fc in ("latency", "retry_exhaustion"):
                return True
            return bool("retry" in text or "latency" in text or "timeout" in text)

        if _is_unknown_command():
            artifact_type, target = "routing_rule", "unknown_command_handler"
            aliases = get_command_aliases()
            # Derive aliases from actual recent unknown commands.
            # Three-pass approach: known corrections map → prefix match → edit distance fallback.
            # This handles semantic synonyms (cronstatus→cronlist) that prefix matching cannot reach.
            _KNOWN_CORRECTIONS: dict[str, str] = {
                "cronstatus": "cronlist",
                "cronstat": "cronlist",
                "improvepolicy": "improve_policy",
                "inproving": "improving",
                "inprovepolicy": "improve_policy",
                "statuscheck": "status",
                "skillpolicy": "skills",
                "improveartifact": "improve_artifacts",
                "evolvephase": "improve_phase",
                "improvecompile": "improve_compile",
                "improvesimulate": "improve_simulate",
                "improvepromote": "improve_promote",
            }

            def _edit_distance(a: str, b: str) -> int:
                if len(a) < len(b):
                    return _edit_distance(b, a)
                if not b:
                    return len(a)
                prev = list(range(len(b) + 1))
                for ca in a:
                    curr = [prev[0] + 1]
                    for j, cb in enumerate(b):
                        curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
                    prev = curr
                return prev[-1]

            _recent_unknown = conn.execute(
                "SELECT COALESCE(command,'') AS cmd FROM op_events "
                "WHERE event_type='unknown_command' AND timestamp >= datetime('now','-7 day') "
                "AND COALESCE(command,'') NOT IN ('','/unknowncommand') "
                "GROUP BY COALESCE(command,'') ORDER BY COUNT(*) DESC LIMIT 10"
            ).fetchall()
            _known_targets = sorted(set(aliases.values()))
            for _row in _recent_unknown:
                _cmd = str(_row["cmd"] or "").strip().lstrip("/")
                if not _cmd or len(_cmd) < 3:
                    continue
                # Pass 0: known corrections map
                if _cmd in _KNOWN_CORRECTIONS:
                    aliases.setdefault(_cmd, _KNOWN_CORRECTIONS[_cmd])
                    continue
                # Pass 1: prefix match
                _matched = False
                for _target in _known_targets:
                    if _target.startswith(_cmd) and _cmd != _target:
                        aliases.setdefault(_cmd, _target)
                        _matched = True
                        break
                if _matched:
                    continue
                # Pass 2: edit distance fallback (max distance 3)
                if _known_targets:
                    _best = min(_known_targets, key=lambda t: _edit_distance(_cmd, t))
                    if _edit_distance(_cmd, _best) <= 3:
                        aliases.setdefault(_cmd, _best)
            artifact = {"add_alias": aliases}; expected_metric = "unknown_command_rate"; expected_delta = -0.15
        elif _is_route_instability():
            # Discriminate blocked vs error before choosing artifact.
            # If policy blocks dominate, retrying won't help — use skill_policy_change.
            _rm = (
                re.search(r"failing route[:\s'\"]+([a-z_]+)", str(p["title"] or "").lower()) or
                re.search(r"(?:for\s+|route\s*[:'\"]\s*)([a-z][a-z_]{2,})\b", text) or
                re.search(r"\b([a-z][a-z_]{2,})\s+route\b", text)
            )
            _route_name = _rm.group(1) if _rm else ""
            _blocked = _errored = 0
            # Prefer pre-built snapshot counts over live DB queries when available.
            _snap_routes = (diagnosis_snapshot or {}).get("top_failing_routes", [])
            _snap_match = next(
                (r for r in _snap_routes if r.get("route") and (r["route"] == _route_name or r["route"] in text)),
                None,
            )
            if _snap_match:
                _route_name = _route_name or str(_snap_match["route"])
                _blocked = int(_snap_match.get("blocked_count", 0))
                _errored = int(_snap_match.get("error_count", 0))
            elif _route_name:
                _blocked = int(conn.execute(
                    "SELECT COUNT(*) AS n FROM op_events WHERE route=? AND status='blocked' AND timestamp >= datetime('now','-7 day')",
                    (_route_name,),
                ).fetchone()["n"] or 0)
                _errored = int(conn.execute(
                    "SELECT COUNT(*) AS n FROM op_events WHERE route=? AND status NOT IN ('ok','skipped','blocked') AND timestamp >= datetime('now','-7 day')",
                    (_route_name,),
                ).fetchone()["n"] or 0)
            if _blocked > _errored:
                artifact_type, target = "skill_policy_change", "runtime_skill_config"
                artifact = {"skill": _route_name or "run_shell", "policy": "max_calls_per_hour", "value": "120"}
                expected_metric = "failure_rate"; expected_delta = -0.05
            else:
                artifact_type, target = "config_change", "runtime_retry_policy"
                cur = get_exec_retry_limit()
                artifact = {"key": "exec_retry_limit", "value": str(max(0, min(3, cur + 1)))}
                expected_metric = "failure_rate"; expected_delta = -0.10
        elif _is_latency_or_retry():
            artifact_type, target = "config_change", "runtime_retry_policy"
            cur = get_exec_retry_limit(); artifact = {"key": "exec_retry_limit", "value": str(max(0, min(3, cur + 1)))}; expected_metric = "failure_rate"; expected_delta = -0.10
        elif "snapshot lineage" in text or "last_applied_artifact_id" in text or "lineage" in text:
            artifact_type, target = "state_lineage", "runtime_snapshot"
            artifact = {"artifact_id": int(get_last_applied_artifact_id() or 0)}; expected_metric = "state_consistency"; expected_delta = 0.0
        elif "prompt" in text:
            artifact_type, target = "prompt_change", "runtime_prompt"
            mode = get_mode(); cur_prompt = get_runtime_prompt(mode)
            artifact = {"mode": mode, "value": (cur_prompt + "\n\n[Improvement candidate]\n- Keep responses concise, factual, and tool-aware.")[:12000]}
            expected_metric = "golden_trace_pass_rate"; expected_delta = 0.0
        elif any(k in text for k in ["skill policy", "allow domain", "allow domains", "write root", "rate limit", "max_calls_per_hour"]):
            artifact_type, target = "skill_policy_change", "runtime_skill_config"
            skill = "http_get" if "domain" in text else ("run_shell" if "write root" in text else "run_shell")
            if "domain" in text:
                artifact = {"skill": skill, "policy": "allow_domains", "value": "*"}
            elif "write root" in text:
                artifact = {"skill": skill, "policy": "write_root", "value": "/home/xavi/.openclaw/workspace"}
            else:
                artifact = {"skill": "run_shell", "policy": "max_calls_per_hour", "value": "120"}
            expected_metric = "failure_rate"; expected_delta = -0.05
        elif "auto_sig:" in text or "auto_failure_cluster" in text:
            # Autonomous failure clusters must compile into a deterministic low-risk artifact
            # to unlock full auto-loop (compile -> simulate -> promote).
            artifact_type, target = "config_change", "runtime_retry_policy"
            cur = get_exec_retry_limit()
            artifact = {"key": "exec_retry_limit", "value": str(max(0, min(3, cur + 1)))}
            expected_metric = "failure_rate"; expected_delta = -0.05
        else:
            return f"⛔ Proposal #{proposal_id} is non-actionable: cannot compile to allowed artifact type."

        # No-op guard: skip artifact creation if the proposed value matches current state.
        if artifact_type == "config_change" and target == "runtime_retry_policy":
            _proposed_val = str(artifact.get("value", ""))
            if _proposed_val == str(get_exec_retry_limit()):
                return f"⛔ Proposal #{proposal_id} is non-actionable: exec_retry_limit already at {_proposed_val}, no change would be made."

        contract = artifact_contract(artifact_type, target)
        artifact["apply_points"] = contract.get("apply_points", [])
        artifact["invariants"] = contract.get("invariants", [])
        artifact["contract_version"] = contract.get("contract_version", "")
        artifact["contract_hash"] = contract.get("contract_hash", "")
        family = str(contract.get("family", "unknown"))
        if family not in allowed_artifact_families():
            return f"⛔ Artifact family '{family}' is frozen/disabled by trial scope."

        cur = conn.execute("INSERT INTO change_artifacts(proposal_id,artifact_type,target,artifact_json,expected_metric,expected_delta,status) VALUES(?,?,?,?,?,?,'compiled')", (proposal_id, artifact_type, target, json.dumps(artifact), expected_metric, expected_delta))
        aid = int(cur.lastrowid)
    return f"Artifact #{aid} compiled for proposal #{proposal_id}: {artifact_type} -> {target}"
