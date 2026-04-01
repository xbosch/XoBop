from __future__ import annotations

import json
from typing import Callable, Optional


def impact_compare_sync(*, proposal_id: int, window_days: int, get_db: Callable, get_eval_phase: Callable[[], str]) -> str:
    w = max(1, min(window_days, 30))
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,created_ts,title,risk_level FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

        ts = p["created_ts"]
        a = conn.execute(
            "SELECT id,applied_ts,commit_hash FROM change_artifacts WHERE proposal_id=? AND applied_ts IS NOT NULL ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        if a and a["applied_ts"]:
            ts = a["applied_ts"]

        before = conn.execute(
            """
            SELECT
              COUNT(*) AS events,
              SUM(CASE WHEN status NOT IN ('ok','skipped') THEN 1 ELSE 0 END) AS failures,
              SUM(CASE WHEN event_type='unknown_command' THEN 1 ELSE 0 END) AS unknowns,
              AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms>0 THEN latency_ms END) AS avg_latency
            FROM op_events
            WHERE timestamp >= datetime(?, ?)
              AND timestamp < datetime(?)
            """,
            (ts, f"-{w} day", ts),
        ).fetchone()

        after = conn.execute(
            """
            SELECT
              COUNT(*) AS events,
              SUM(CASE WHEN status NOT IN ('ok','skipped') THEN 1 ELSE 0 END) AS failures,
              SUM(CASE WHEN event_type='unknown_command' THEN 1 ELSE 0 END) AS unknowns,
              AVG(CASE WHEN latency_ms IS NOT NULL AND latency_ms>0 THEN latency_ms END) AS avg_latency
            FROM op_events
            WHERE timestamp >= datetime(?)
              AND timestamp < datetime(?, ?)
            """,
            (ts, ts, f"+{w} day"),
        ).fetchone()

        def _num(row, key):
            v = row[key]
            return float(v) if v is not None else 0.0

        b_events = _num(before, "events")
        a_events = _num(after, "events")
        b_fail = _num(before, "failures")
        a_fail = _num(after, "failures")
        b_unk = _num(before, "unknowns")
        a_unk = _num(after, "unknowns")
        b_lat = _num(before, "avg_latency")
        a_lat = _num(after, "avg_latency")

        b_rate = (100.0 * b_fail / b_events) if b_events else 0.0
        a_rate = (100.0 * a_fail / a_events) if a_events else 0.0

        delta_fail_rate = a_rate - b_rate
        delta_unknown = a_unk - b_unk
        delta_lat = a_lat - b_lat

        art = conn.execute(
            "SELECT artifact_type,target FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        artifact_type = (art["artifact_type"] if art else "") or ""

        family = {}
        if artifact_type == "routing_rule":
            b_uk = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime(?, ?) AND timestamp < datetime(?) AND event_type='unknown_command'",
                (ts, f"-{w} day", ts),
            ).fetchone()["n"]
            a_uk = conn.execute(
                "SELECT COUNT(*) AS n FROM op_events WHERE timestamp >= datetime(?) AND timestamp < datetime(?, ?) AND event_type='unknown_command'",
                (ts, ts, f"+{w} day"),
            ).fetchone()["n"]
            family = {"family": "alias", "unknown_command_before": int(b_uk or 0), "unknown_command_after": int(a_uk or 0), "delta": int(a_uk or 0) - int(b_uk or 0)}
        elif artifact_type == "config_change":
            family = {"family": "retry", "fail_rate_delta": delta_fail_rate, "avg_latency_delta": delta_lat}

        min_samples = 20
        confidence = "high" if min(b_events, a_events) >= min_samples else "low"

        summary = {
            "window_days": w,
            "before": {"events": b_events, "fail_rate": b_rate, "unknowns": b_unk, "avg_latency": b_lat},
            "after": {"events": a_events, "fail_rate": a_rate, "unknowns": a_unk, "avg_latency": a_lat},
            "delta": {"fail_rate": delta_fail_rate, "unknowns": delta_unknown, "avg_latency": delta_lat},
            "artifact_family": family,
            "confidence": confidence,
            "min_samples_required": min_samples,
        }

        status = "ok"
        if a_events <= 0:
            status = "insufficient"

        conn.execute(
            """
            INSERT INTO improvement_runs(proposal_id,phase,status,eval_phase,before_json,after_json,notes)
            VALUES(?, 'impact_compare', ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                status,
                get_eval_phase(),
                json.dumps(summary.get("before", {})),
                json.dumps(summary.get("after", {})),
                f"delta={json.dumps(summary.get('delta', {}))}",
            ),
        )

    lines = [
        f"Impact compare for proposal #{proposal_id} (window={w}d)",
        f"Title: {p['title']} [{p['risk_level']}]",
        f"Anchor: {'artifact deployment' if (a and a['applied_ts']) else 'proposal creation'} @ {ts}",
        f"Before: events={int(b_events)} fail_rate={b_rate:.1f}% unknown={int(b_unk)} avg_lat={b_lat:.1f}ms",
        f"After:  events={int(a_events)} fail_rate={a_rate:.1f}% unknown={int(a_unk)} avg_lat={a_lat:.1f}ms",
        f"Delta:  fail_rate={delta_fail_rate:+.1f}pp unknown={delta_unknown:+.0f} avg_lat={delta_lat:+.1f}ms",
    ]
    if a_events <= 0:
        lines.append("Assessment: insufficient post-proposal data yet.")
    else:
        improved = (delta_fail_rate <= 0.0) and (delta_unknown <= 0.0)
        lines.append(f"Assessment: {'improved' if improved else 'mixed/regressed'}")
    if min(b_events, a_events) < 20:
        lines.append("Confidence: LOW (sample threshold not met)")
    else:
        lines.append("Confidence: HIGH")
    return "\n".join(lines)


def latest_run_status(*, proposal_id: int, phase: str, get_db: Callable) -> Optional[str]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM improvement_runs WHERE proposal_id=? AND phase=? ORDER BY id DESC LIMIT 1",
            (proposal_id, phase),
        ).fetchone()
    return row["status"] if row else None


def latest_impact_delta(*, proposal_id: int, get_db: Callable) -> dict[str, float]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT notes FROM improvement_runs WHERE proposal_id=? AND phase='impact_compare' ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    if not row:
        return {}
    notes = (row["notes"] or "").strip()
    if notes.startswith("delta="):
        try:
            d = json.loads(notes[len("delta="):])
            if isinstance(d, dict):
                return {str(k): float(v) for k, v in d.items() if isinstance(v, (int, float))}
        except Exception:
            pass
    return {}


def auto_promote_low_sync(*, get_improve_policy: Callable[[], str], get_db: Callable, promote_proposal_sync: Callable[[int, bool, str], str]) -> str:
    policy = get_improve_policy()
    if policy != "auto_low":
        return f"⛔ Auto-promotion disabled by policy={policy}."

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM improvement_proposals WHERE status='proposed' AND risk_level='low' ORDER BY id ASC LIMIT 20"
        ).fetchall()

    promoted = 0
    blocked = 0
    for r in rows:
        pid = int(r["id"])
        msg = promote_proposal_sync(pid, False, "")
        if msg.startswith("✅"):
            promoted += 1
        else:
            blocked += 1

    return f"Auto-promotion complete: promoted={promoted} blocked={blocked}"


def capture_evolution_memory_sync(*, proposal_id: int, get_db: Callable) -> str:
    with get_db() as conn:
        p = conn.execute(
            "SELECT id,title,risk_level,status,problem_statement,suspected_cause,metric_target FROM improvement_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not p:
            raise ValueError(f"Proposal not found: {proposal_id}")

        runs = conn.execute(
            "SELECT phase,status,notes,created_ts,before_json,after_json FROM improvement_runs WHERE proposal_id=? ORDER BY id ASC",
            (proposal_id,),
        ).fetchall()
        art = conn.execute(
            "SELECT id,artifact_type,target,artifact_json,applied_ts,commit_hash FROM change_artifacts WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()

        sim = [r for r in runs if r["phase"] == "simulation"]
        imp = [r for r in runs if r["phase"] == "impact_compare"]
        promo = [r for r in runs if r["phase"] == "promotion"]

        sim_ok = any(r["status"] == "ok" for r in sim)
        imp_err = any(r["status"] == "error" for r in imp)
        promoted = any(r["status"] == "ok" for r in promo) or p["status"] == "promoted"

        outcome = "promoted" if promoted else ("regressed" if imp_err else ("sim_pass" if sim_ok else "proposed"))
        signature = f"risk:{p['risk_level']}|outcome:{outcome}"

        if imp_err:
            lesson = "Impact comparison showed regression; require stronger guardrails or rollback before promotion."
        elif sim_ok and not promoted:
            lesson = "Simulation passed but proposal not promoted; policy/risk gate intentionally held rollout."
        elif promoted:
            lesson = "Proposal cleared simulation/policy and was promoted; reuse this change pattern when similar diagnostics recur."
        else:
            lesson = "Proposal is still in analysis stage; gather more telemetry before action."

        last_imp = imp[-1] if imp else None
        evidence = {
            "title": p["title"],
            "risk_level": p["risk_level"],
            "status": p["status"],
            "problem": p["problem_statement"],
            "cause": p["suspected_cause"],
            "metric_target": p["metric_target"],
            "run_count": len(runs),
            "artifact": {
                "id": int(art["id"]) if art else None,
                "type": art["artifact_type"] if art else None,
                "target": art["target"] if art else None,
                "change": json.loads(art["artifact_json"] or "{}") if art else {},
                "applied_ts": art["applied_ts"] if art else None,
                "commit_hash": art["commit_hash"] if art else None,
            },
            "impact_notes": (last_imp["notes"] if last_imp else ""),
        }

        conn.execute(
            """
            INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,evidence_json)
            VALUES(?,?,?,?,?)
            """,
            (proposal_id, signature, outcome, lesson, json.dumps(evidence)),
        )

    return f"Evolution memory captured for proposal #{proposal_id}: {signature}"


def evolution_memory_report(*, limit: int, get_db: Callable) -> str:
    n = max(1, min(limit, 30))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,created_ts,proposal_id,signature,outcome,lesson FROM evolution_memory ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    if not rows:
        return "No evolution memory entries yet."
    lines = []
    for r in rows:
        lines.append(f"#{int(r['id'])} p#{int(r['proposal_id'] or 0)} {r['signature']} [{r['outcome']}]")
        lines.append(f"  {r['lesson']}")
    return "\n".join(lines)


def improve_funnel_report(*, days: int, get_db: Callable) -> str:
    d = max(1, min(days, 90))
    with get_db() as conn:
        generated = conn.execute("SELECT COUNT(*) AS n FROM improvement_proposals WHERE created_ts >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        actionable = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        artifacts_compiled = conn.execute("SELECT COUNT(*) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        sim_pass = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='simulation' AND status='ok' AND created_ts >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        promoted = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND created_ts >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        promoted_force = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND promotion_mode='force' AND created_ts >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        promoted_auto = max(0, int(promoted or 0) - int(promoted_force or 0))

        impact_rows = conn.execute(
            """
            SELECT r.status, r.notes, p.promotion_mode
            FROM improvement_runs r
            LEFT JOIN (
              SELECT proposal_id, MAX(id) AS rid FROM improvement_runs WHERE phase='promotion' AND status='ok' GROUP BY proposal_id
            ) lp ON lp.proposal_id = r.proposal_id
            LEFT JOIN improvement_runs p ON p.id = lp.rid
            WHERE r.phase='impact_compare' AND r.created_ts >= datetime('now', ?)
            """,
            (f"-{d} day",),
        ).fetchall()

        drift_repairs = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='state_repair' AND timestamp >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]

        family_rows = conn.execute("SELECT artifact_type, COUNT(*) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?) GROUP BY artifact_type", (f"-{d} day",)).fetchall()

    positive = neutral = negative = 0
    positive_auto = neutral_auto = negative_auto = 0
    for r in impact_rows:
        if r["status"] != "ok":
            neutral += 1
            if (r["promotion_mode"] or "") != "force":
                neutral_auto += 1
            continue
        note = (r["notes"] or "")
        dlt = {}
        if note.startswith("delta="):
            try:
                dlt = json.loads(note[len("delta="):])
            except Exception:
                dlt = {}
        fail_delta = float(dlt.get("fail_rate", 0.0)) if isinstance(dlt, dict) else 0.0
        if fail_delta < 0:
            positive += 1
            if (r["promotion_mode"] or "") != "force":
                positive_auto += 1
        elif fail_delta > 0:
            negative += 1
            if (r["promotion_mode"] or "") != "force":
                negative_auto += 1
        else:
            neutral += 1
            if (r["promotion_mode"] or "") != "force":
                neutral_auto += 1

    actionable_rate = (100.0 * actionable / generated) if generated else 0.0
    success_rate = (100.0 * positive / promoted) if promoted else 0.0
    success_rate_auto = (100.0 * positive_auto / promoted_auto) if promoted_auto else 0.0
    fam = ", ".join([f"{r['artifact_type']}={int(r['n'])}" for r in family_rows]) or "none"
    return (
        f"Improve funnel ({d}d)\n"
        f"• proposals_generated: {int(generated)}\n"
        f"• proposals_actionable: {int(actionable)} ({actionable_rate:.1f}%)\n"
        f"• actionable_yield: {actionable_rate:.1f}%\n"
        f"• artifacts_compiled: {int(artifacts_compiled)}\n"
        f"• simulations_passed: {int(sim_pass)}\n"
        f"• promotions_applied_total: {int(promoted)}\n"
        f"• promotions_auto: {int(promoted_auto)}\n"
        f"• promotions_force: {int(promoted_force)}\n"
        f"• force_ratio: {(100.0*promoted_force/promoted if promoted else 0.0):.1f}%\n"
        f"• changes_positive_impact_all: {int(positive)} (of applied: {success_rate:.1f}%)\n"
        f"• changes_positive_impact_auto: {int(positive_auto)} (of auto: {success_rate_auto:.1f}%)\n"
        f"• promotion_effectiveness: {success_rate_auto:.1f}%\n"
        f"• changes_neutral_all: {int(neutral)}\n"
        f"• changes_negative_all: {int(negative)}\n"
        f"• changes_neutral_auto: {int(neutral_auto)}\n"
        f"• changes_negative_auto: {int(negative_auto)}\n"
        f"• drift_repair_events: {int(drift_repairs)}\n"
        f"• artifact_families: {fam}"
    )


def improve_weekly_report(*, weeks: int, get_db: Callable, get_eval_phase: Callable[[], str], get_improve_policy: Callable[[], str]) -> str:
    w = max(1, min(weeks, 8))
    lines = [f"Improve weekly report ({w}w) phase={get_eval_phase()} policy={get_improve_policy()}"]
    with get_db() as conn:
        for i in range(w):
            end = f"-{7*i} day"
            start = f"-{7*(i+1)} day"
            req = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='command_requested' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)", (start, end)).fetchone()["n"]
            fail = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE status NOT IN ('ok','skipped') AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)", (start, end)).fetchone()["n"]
            unk = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='unknown_command' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)", (start, end)).fetchone()["n"]
            proposals = conn.execute("SELECT COUNT(*) AS n FROM improvement_proposals WHERE created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)", (start, end)).fetchone()["n"]
            actionable = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM change_artifacts WHERE created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)", (start, end)).fetchone()["n"]
            sim_pass = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='simulation' AND status='ok' AND created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)", (start, end)).fetchone()["n"]
            promoted = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)", (start, end)).fetchone()["n"]
            promoted_force = conn.execute("SELECT COUNT(DISTINCT proposal_id) AS n FROM improvement_runs WHERE phase='promotion' AND status='ok' AND promotion_mode='force' AND created_ts >= datetime('now', ?) AND created_ts < datetime('now', ?)", (start, end)).fetchone()["n"]
            promoted_auto = max(0, int(promoted or 0) - int(promoted_force or 0))
            drift = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='state_repair' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)", (start, end)).fetchone()["n"]
            rollback = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='rollback_event' AND timestamp >= datetime('now', ?) AND timestamp < datetime('now', ?)", (start, end)).fetchone()["n"]
            lines.append(f"• w-{i+1}: req={int(req)} fail={int(fail)} unk={int(unk)} prop={int(proposals)} actionable={int(actionable)} sim_pass={int(sim_pass)} promoted={int(promoted)} auto={int(promoted_auto)} force={int(promoted_force)} drift_repair={int(drift)} rollback={int(rollback)}")
    return "\n".join(lines)


def improve_eval_report(*, days: int, get_db: Callable, get_eval_phase: Callable[[], str], get_improve_policy: Callable[[], str], improve_funnel_report_fn: Callable[[int], str], improve_weekly_report_fn: Callable[[int], str]) -> str:
    d = max(1, min(days, 90))
    with get_db() as conn:
        req = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='command_requested' AND timestamp >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        fail = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE status NOT IN ('ok','skipped') AND timestamp >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        unk = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='unknown_command' AND timestamp >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]
        repairs = conn.execute("SELECT COUNT(*) AS n FROM op_events WHERE event_type='state_repair' AND timestamp >= datetime('now', ?)", (f"-{d} day",)).fetchone()["n"]

    fail_rate = (100.0 * fail / req) if req else 0.0
    lines = [
        f"Evaluation report ({d}d)",
        f"• phase: {get_eval_phase()} policy: {get_improve_policy()}",
        f"• requests: {int(req)}",
        f"• failures: {int(fail)} ({fail_rate:.1f}%)",
        f"• unknown_command: {int(unk)}",
        f"• state_repair_events: {int(repairs)}",
        "",
        improve_funnel_report_fn(d),
        "",
        improve_weekly_report_fn(min(8, max(1, d // 7))),
    ]
    return "\n".join(lines)
