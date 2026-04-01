from __future__ import annotations

import json
from typing import Any, Callable


def diagnose_report(*, days: int, get_db: Callable) -> str:
    d = max(1, min(days, 30))
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
            SELECT COALESCE(route,'') AS route, COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND status NOT IN ('ok','skipped')
            GROUP BY COALESCE(route,'')
            ORDER BY n DESC
            LIMIT 5
            """,
            (f"-{d} day",),
        ).fetchall()
        unknown_cmds = conn.execute(
            """
            SELECT COALESCE(command,'') AS cmd, COUNT(*) AS n
            FROM op_events
            WHERE timestamp >= datetime('now', ?)
              AND event_type='unknown_command'
            GROUP BY COALESCE(command,'')
            ORDER BY n DESC
            LIMIT 5
            """,
            (f"-{d} day",),
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
        lines.append("Top failing routes:")
        for r in fail_routes:
            lines.append(f"- {(r['route'] or '(none)')}: {int(r['n'])}")
    if unknown_cmds:
        lines.append("Top unknown commands:")
        for r in unknown_cmds:
            lines.append(f"- {(r['cmd'] or '(empty)')}: {int(r['n'])}")

    likely = []
    if total_unknown >= 3:
        likely.append("Command discoverability issue: high unknown-command volume")
    if any((r["route"] or "") == "run_shell" for r in fail_routes):
        likely.append("Shell route instability: investigate policy blocks/path assumptions")
    if daily and any(float(r["p95_latency_ms"] or 0) > 4000 for r in daily):
        likely.append("Latency pressure: p95 over 4s in diagnosis window")
    if likely:
        lines.append("Likely causes:")
        lines.extend([f"- {x}" for x in likely])
    return "\n".join(lines)


def generate_improvement_proposal(*, days: int, get_db: Callable, diagnose_report_fn: Callable[[int], str]) -> tuple[int, str]:
    d = max(1, min(days, 30))
    _ = diagnose_report_fn(d)
    risk = "low"
    title = "Ops improvement proposal"
    problem = "General reliability friction detected in recent diagnostics."
    cause = "Mixed routing/latency/command UX friction."
    change = "Tune command discoverability and route diagnostics."
    benefit = "Lower failure/unknown-command rates with stable latency."
    metric = "failures down; unknown-command count down; p95 latency stable"

    with get_db() as conn:
        fail_routes = conn.execute("SELECT COALESCE(route,'') AS route, COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) AND status NOT IN ('ok','skipped') GROUP BY COALESCE(route,'') ORDER BY n DESC LIMIT 1", (f"-{d} day",)).fetchone()
        unknown = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime('now', ?) AND event_type='unknown_command'", (f"-{d} day",)).fetchone()["n"]
        p95 = conn.execute("SELECT COALESCE(MAX(p95_latency_ms),0) AS p95 FROM op_signals_daily WHERE day >= date('now', ?)", (f"-{d} day",)).fetchone()["p95"]

        if int(unknown or 0) >= 3:
            title = "Reduce unknown command friction"
            problem = f"Unknown commands observed {int(unknown)} time(s) in last {d} day(s)."
            cause = "Command discoverability/help text mismatch with user inputs."
            change = "Add command alias hints and improve unknown-command suggestion mapping."
            benefit = "Fewer dead-end command attempts and lower correction loops."
            metric = "unknown_command count reduced by >=50% over next 7 days"
        elif fail_routes and (fail_routes["route"] or ""):
            route = fail_routes["route"]
            risk = "medium" if route in {"run_shell", "cmd_exec"} else "low"
            title = f"Stabilize failing route: {route}"
            problem = f"Route '{route}' is top failure source in last {d} day(s)."
            cause = "Route-specific policy/path/validation friction."
            change = f"Add pre-validation and clearer blocked/error responses for route '{route}'."
            benefit = "Lower route failure rate and fewer retries."
            metric = f"failures on route '{route}' reduced by >=40%"
        elif float(p95 or 0) > 4000:
            risk = "medium"
            title = "Reduce p95 latency pressure"
            problem = f"Observed p95 latency spike up to {float(p95):.0f}ms."
            cause = "Tool/LLM path latency accumulation under current flow."
            change = "Add fast-path short-circuiting for deterministic intents and tighter timeout tiers."
            benefit = "Improved responsiveness without reducing correctness."
            metric = "p95 latency < 3000ms while failure rate does not increase"

        cur = conn.execute("INSERT INTO improvement_proposals (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target) VALUES ('proposed',?,?,?,?,?,?,?)", (risk, title, problem, cause, change, benefit, metric))
        pid = int(cur.lastrowid)

    out = f"Proposal #{pid} [{risk}]\nTitle: {title}\nProblem: {problem}\nCause: {cause}\nChange: {change}\nExpected: {benefit}\nMetric target: {metric}\nSource window: last {d} day(s)"
    return pid, out


def compile_proposal_to_change(*, proposal_id: int, get_db: Callable, get_command_aliases: Callable, get_exec_retry_limit: Callable, get_last_applied_artifact_id: Callable, get_mode: Callable, get_runtime_prompt: Callable, artifact_contract: Callable, allowed_artifact_families: Callable) -> str:
    with get_db() as conn:
        p = conn.execute("SELECT id,title,problem_statement,suspected_cause,proposed_change,metric_target FROM improvement_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")
        text = " ".join([str(p["title"] or ""), str(p["problem_statement"] or ""), str(p["suspected_cause"] or ""), str(p["proposed_change"] or ""), str(p["metric_target"] or "")]).lower()
        artifact_type = target = expected_metric = ""
        expected_delta = None
        artifact: dict[str, Any] = {}

        if "unknown command" in text or "alias" in text or "discoverability" in text:
            artifact_type, target = "routing_rule", "unknown_command_handler"
            aliases = get_command_aliases(); aliases.setdefault("cronru", "cronrun"); aliases.setdefault("statu", "status")
            artifact = {"add_alias": aliases}; expected_metric = "unknown_command_rate"; expected_delta = -0.15
        elif "retry" in text or "latency" in text or "timeout" in text:
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
        else:
            return f"⛔ Proposal #{proposal_id} is non-actionable: cannot compile to allowed artifact type."

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
