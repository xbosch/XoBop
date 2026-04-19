from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Optional


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
                f"delta={json.dumps(summary.get('delta', {}))}|confidence={confidence}",
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
        if delta_fail_rate <= -1.0:
            # Meaningful failure rate improvement — primary signal.
            # Unknown command count increasing from tester volume growth does not
            # override a genuine fail_rate improvement.
            assessment = "improved"
        elif delta_fail_rate < -0.5:
            assessment = "improved" if delta_unknown <= 5 else "mixed"
        elif delta_fail_rate <= 0.0:
            assessment = "mixed"
        else:
            assessment = "regressed"
        lines.append(f"Assessment: {assessment}")
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


def _parse_impact_notes(notes: str) -> tuple[dict[str, float], str]:
    """Parse impact_compare notes field. Returns (delta_dict, confidence_str)."""
    parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in (notes or "").strip().split("|") if "=" in p}
    delta: dict[str, float] = {}
    if "delta" in parts:
        try:
            d = json.loads(parts["delta"])
            if isinstance(d, dict):
                delta = {str(k): float(v) for k, v in d.items() if isinstance(v, (int, float))}
        except Exception:
            pass
    confidence = parts.get("confidence", "")
    return delta, confidence


def latest_impact_delta(*, proposal_id: int, get_db: Callable) -> dict[str, float]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT notes FROM improvement_runs WHERE proposal_id=? AND phase='impact_compare' ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    if not row:
        return {}
    delta, _ = _parse_impact_notes(row["notes"] or "")
    return delta


def latest_impact_confidence(*, proposal_id: int, get_db: Callable) -> str:
    """Returns 'high', 'low', or '' if no impact compare run exists."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT notes FROM improvement_runs WHERE proposal_id=? AND phase='impact_compare' ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    if not row:
        return ""
    _, confidence = _parse_impact_notes(row["notes"] or "")
    return confidence


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


def _derive_evolution_fields(proposal_id: int, conn) -> dict:
    """Compute all evolution memory fields for a proposal without writing to DB.

    Returns a dict with keys: outcome, signature, lesson, evidence_json,
    failure_class, artifact_family, target_route, metric_delta, signal_fingerprint.
    Raises ValueError if the proposal does not exist.
    """
    p = conn.execute(
        "SELECT id,title,risk_level,status,problem_statement,suspected_cause,metric_target,failure_class FROM improvement_proposals WHERE id=?",
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

    # Prefer stored failure_class (set at generation time with telemetry context);
    # keyword derivation from title/problem is a fallback for empty/general values.
    title_lower = (p["title"] or "").lower()
    problem_lower = (p["problem_statement"] or "").lower()
    stored_fc = str(p["failure_class"] or "").strip()
    if stored_fc and stored_fc != "general":
        failure_class = stored_fc
    else:
        if "unknown command" in title_lower or "unknown command" in problem_lower or "discoverability" in title_lower:
            failure_class = "unknown_command"
        elif "latency" in title_lower or "latency" in problem_lower or "p95" in title_lower:
            failure_class = "latency"
        elif "stabilize" in title_lower or "instability" in problem_lower or "failing route" in title_lower or ("route" in title_lower and "fail" in title_lower):
            failure_class = "route_failure"
        elif "retry" in title_lower or "retry" in problem_lower:
            failure_class = "retry_exhaustion"
        else:
            failure_class = stored_fc or "general"

    _family_map = {"routing_rule": "routing", "config_change": "config", "prompt_change": "prompt", "skill_policy_change": "skill_policy", "state_lineage": "state"}
    artifact_family = _family_map.get(art["artifact_type"] if art else "", art["artifact_type"] if art else "") or ""

    target_route = str(art["target"] if art else "") or ""
    _rm = re.search(r"(?:route[:\s]+|for route[:\s]+|failing route[:\s]+)([a-z_]+)", title_lower)
    if _rm:
        target_route = _rm.group(1)

    metric_delta: float | None = None
    last_imp = imp[-1] if imp else None
    if last_imp:
        try:
            b_rate = float(json.loads(last_imp["before_json"] or "{}").get("fail_rate", 0) or 0)
            a_rate = float(json.loads(last_imp["after_json"] or "{}").get("fail_rate", 0) or 0)
            if b_rate or a_rate:
                metric_delta = round(a_rate - b_rate, 4)
        except Exception:
            pass

    signal_fingerprint = json.dumps({"failure_class": failure_class, "route": target_route, "artifact_family": artifact_family})
    signature = f"risk:{p['risk_level']}|class:{failure_class}|outcome:{outcome}"

    _d = lambda s: s.replace("_", "-")  # noqa: E731 — sanitize underscores for MarkdownV1
    if imp_err:
        lesson = f"Impact comparison showed regression for {_d(failure_class)} on '{_d(target_route)}'; require stronger guardrails or rollback."
    elif sim_ok and not promoted:
        lesson = f"Simulation passed but not promoted ({_d(failure_class)} on '{_d(target_route)}'); policy/risk gate held rollout."
    elif promoted:
        lesson = f"Promoted: {_d(artifact_family)} addressed {_d(failure_class)} on '{_d(target_route)}'. Reuse when similar signals recur."
    else:
        lesson = f"Still in analysis ({_d(failure_class)} on '{_d(target_route)}'); gather more telemetry."

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

    return {
        "outcome": outcome,
        "signature": signature,
        "lesson": lesson,
        "evidence_json": json.dumps(evidence),
        "failure_class": failure_class,
        "artifact_family": artifact_family,
        "target_route": target_route,
        "metric_delta": metric_delta,
        "signal_fingerprint": signal_fingerprint,
    }


def capture_evolution_memory_sync(*, proposal_id: int, get_db: Callable) -> str:
    with get_db() as conn:
        fields = _derive_evolution_fields(proposal_id, conn)
        _em_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        if "artifact_family" in _em_cols:
            conn.execute(
                """
                INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,evidence_json,
                                             artifact_family,target_route,metric_delta,failure_class,signal_fingerprint)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (proposal_id, fields["signature"], fields["outcome"], fields["lesson"], fields["evidence_json"],
                 fields["artifact_family"], fields["target_route"], fields["metric_delta"],
                 fields["failure_class"], fields["signal_fingerprint"]),
            )
        else:
            conn.execute(
                "INSERT INTO evolution_memory(proposal_id,signature,outcome,lesson,evidence_json) VALUES(?,?,?,?,?)",
                (proposal_id, fields["signature"], fields["outcome"], fields["lesson"], fields["evidence_json"]),
            )
    return f"Evolution memory captured for proposal #{proposal_id}: {fields['signature']}"


def recapture_all_evolution_sync(*, get_db: Callable, capture_fn: Callable[[int], str]) -> str:
    """Refresh v2 fields and lessons on existing evolution_memory rows for all promoted proposals.

    UPDATEs in-place rather than inserting duplicates. Falls back to capture_fn (INSERT)
    only if no existing row is found for a proposal.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM improvement_proposals WHERE status='promoted' ORDER BY id ASC"
        ).fetchall()
    pids = [int(r["id"]) for r in rows]
    if not pids:
        return "No promoted proposals to recapture."
    ok_ids: list[int] = []
    fail_ids: list[tuple[int, str]] = []
    for pid in pids:
        try:
            with get_db() as conn:
                _em_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
                fields = _derive_evolution_fields(pid, conn)
                existing = conn.execute(
                    "SELECT id FROM evolution_memory WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
                    (pid,),
                ).fetchone()
                if existing and "artifact_family" in _em_cols:
                    conn.execute(
                        """
                        UPDATE evolution_memory
                        SET outcome=?, signature=?, lesson=?, evidence_json=?,
                            failure_class=?, artifact_family=?, target_route=?,
                            metric_delta=?, signal_fingerprint=?
                        WHERE id=?
                        """,
                        (fields["outcome"], fields["signature"], fields["lesson"], fields["evidence_json"],
                         fields["failure_class"], fields["artifact_family"], fields["target_route"],
                         fields["metric_delta"], fields["signal_fingerprint"], int(existing["id"])),
                    )
                elif not existing:
                    # No row yet — do a fresh INSERT via capture_fn
                    capture_fn(pid)
            ok_ids.append(pid)
        except Exception as exc:
            fail_ids.append((pid, f"{type(exc).__name__}: {exc}"))
    parts = [f"Recaptured {len(ok_ids)}/{len(pids)} promoted proposals."]
    if ok_ids:
        parts.append("✓ " + " ".join(f"p#{i}" for i in ok_ids))
    for pid, err in fail_ids:
        parts.append(f"✗ p#{pid}: {err}")
    return "\n".join(parts)


def lookup_evolution_memory(*, get_db: Callable, failure_class: str, target_route: str = "", limit: int = 3) -> list[dict[str, Any]]:
    """Return recent promoted entries matching failure_class, optionally narrowed to target_route.

    Returns empty list if v2 columns are not yet migrated or no matching entries exist.
    """
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        if "failure_class" not in cols:
            return []
        if target_route:
            rows = conn.execute(
                """
                SELECT artifact_family, target_route, metric_delta, failure_class, lesson
                FROM evolution_memory
                WHERE failure_class=? AND (target_route=? OR target_route='') AND outcome='promoted'
                ORDER BY id DESC LIMIT ?
                """,
                (failure_class, target_route, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT artifact_family, target_route, metric_delta, failure_class, lesson
                FROM evolution_memory
                WHERE failure_class=? AND outcome='promoted'
                ORDER BY id DESC LIMIT ?
                """,
                (failure_class, int(limit)),
            ).fetchall()
    return [dict(r) for r in rows]


def evolution_memory_report(*, limit: int, get_db: Callable) -> str:
    n = max(1, min(limit, 30))
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        has_v2 = "failure_class" in cols
        if has_v2:
            rows = conn.execute(
                "SELECT id,created_ts,proposal_id,outcome,lesson,failure_class,artifact_family,target_route,metric_delta FROM evolution_memory ORDER BY id DESC LIMIT ?",
                (n,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,created_ts,proposal_id,signature,outcome,lesson FROM evolution_memory ORDER BY id DESC LIMIT ?",
                (n,),
            ).fetchall()
    if not rows:
        return "No evolution memory entries yet."
    lines = []
    for r in rows:
        header = f"#{int(r['id'])} p#{int(r['proposal_id'] or 0)} [{r['outcome']}]"
        if has_v2:
            fc = str(r["failure_class"] or "?")
            af = str(r["artifact_family"] or "?")
            tr = str(r["target_route"] or "?")
            md = r["metric_delta"]
            delta_str = f" Δ{float(md):+.2f}pp" if md is not None else ""
            header += f" `{fc}`/`{af}` on `{tr}`{delta_str}"
        else:
            header += f" {r['signature']}"
        lines.append(header)
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


# ---------------------------------------------------------------------------
# Diagnosis-conditioned evolution memory retrieval
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Change 1: Learned query vectors per failure class (AttnRes §3.1 analogue)
#
# Math: the governance mixing matrix G where G_{t'→t} is the weight proposal
# cycle t assigns to past promotion t'. Under uniform retrieval G is all-ones
# lower-triangular (standard residuals). Under diagnosis-conditioned retrieval
# G is input-dependent (Full AttnRes). The learned query vector moves G toward
# entries that produced good proposals.
#
# Update rule after each /evolve_capture:
#   q_{t+1} = normalize( (1-α)*q_t  +  α * sign * d_t )
#
# where d_t is the diagnosis embedding at cycle t, α=0.15 is the EMA rate,
# and sign ∈ {+1, -1, +0.5} encodes outcome quality.
#
# sign = +1.0  if metric_delta < -0.5  (meaningful failure rate improvement)
# sign = +0.5  if promoted but no metric data yet
# sign = -1.0  if metric_delta > +0.5  (regression)
# sign =  0.0  otherwise (no update)
#
# The vector is normalised to the unit sphere after each update so that
# cosine similarity scores remain in [-1, 1] regardless of update count.
# ---------------------------------------------------------------------------

async def update_learned_query_vector(
    *,
    failure_class: str,
    diagnosis_embedding: list[float],
    metric_delta: float | None,
    outcome: str,
    get_db: Callable,
    ema_alpha: float = 0.15,
) -> None:
    """EMA update of the per-failure-class learned query vector.

    Called after every /evolve_capture so the query vector accumulates
    signal about which diagnosis patterns produce useful proposals.
    Requires migration 12 (evolution_query_vectors table).
    """
    if not failure_class or not diagnosis_embedding:
        return

    if metric_delta is not None:
        if metric_delta < -0.5:
            sign = 1.0
        elif metric_delta > 0.5:
            sign = -1.0
        else:
            sign = 0.3
    elif outcome == "promoted":
        sign = 0.5
    else:
        sign = 0.0

    if sign == 0.0:
        return

    d = diagnosis_embedding

    with get_db() as conn:
        qv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_query_vectors)").fetchall()}
        if "query_vector_json" not in qv_cols:
            return

        row = conn.execute(
            "SELECT query_vector_json, update_count FROM evolution_query_vectors WHERE failure_class=?",
            (failure_class,),
        ).fetchone()

        if row:
            q = json.loads(row["query_vector_json"])
            count = int(row["update_count"] or 0)
            new_q = [(1.0 - ema_alpha) * qi + ema_alpha * sign * di for qi, di in zip(q, d)]
            norm = math.sqrt(sum(x * x for x in new_q))
            if norm > 0:
                new_q = [x / norm for x in new_q]
            conn.execute(
                "UPDATE evolution_query_vectors "
                "SET query_vector_json=?, update_count=?, last_updated_ts=CURRENT_TIMESTAMP "
                "WHERE failure_class=?",
                (json.dumps(new_q), count + 1, failure_class),
            )
        else:
            new_q = [sign * di for di in d]
            norm = math.sqrt(sum(x * x for x in new_q))
            if norm > 0:
                new_q = [x / norm for x in new_q]
            conn.execute(
                "INSERT INTO evolution_query_vectors (failure_class, query_vector_json, update_count) "
                "VALUES (?,?,1)",
                (failure_class, json.dumps(new_q)),
            )


# ---------------------------------------------------------------------------
# Change 2: Block compression of proposal history (AttnRes §3.2 analogue)
#
# Partition evolution memory into N=4 temporal blocks. Each block is a
# contiguous time-ordered slice of entries compressed to a single mean
# embedding (L2-normalised). The effective rank of G lies between N and
# N+S, interpolating between uniform retrieval (N=1) and full conditioned
# retrieval (N=T).
#
# Block count N=4 is chosen conservatively for the current entry count.
# As history grows, increase N via settings without schema changes.
# Empirically, AttnRes finds N≈8 recovers most of the full-attention gain;
# for XoBop's smaller history N=4 is sufficient.
# ---------------------------------------------------------------------------

async def recompute_evolution_blocks(
    *,
    get_db: Callable,
    n_blocks: int = 4,
) -> int:
    """Recompute temporal block summaries over all evolution entries.

    Each block covers a contiguous time window of evolution entries.
    Block embedding = L2-normalised mean of all entry embeddings in the block.
    Returns the number of blocks written.
    Requires migration 13 (evolution_memory_blocks table).
    """
    with get_db() as conn:
        em_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        if "embedding_json" not in em_cols:
            return 0
        rows = conn.execute(
            "SELECT id, failure_class, artifact_family, metric_delta, embedding_json, created_ts "
            "FROM evolution_memory ORDER BY id ASC"
        ).fetchall()

    if not rows:
        return 0

    total = len(rows)
    block_size = max(1, (total + n_blocks - 1) // n_blocks)
    chunks: list[list] = [list(rows[i : i + block_size]) for i in range(0, total, block_size)]

    written = 0
    with get_db() as conn:
        bk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory_blocks)").fetchall()}
        if "block_embedding_json" not in bk_cols:
            return 0
        conn.execute("DELETE FROM evolution_memory_blocks")

        for block_idx, chunk in enumerate(chunks):
            entry_ids = [int(r["id"]) for r in chunk]
            embeddings: list[list[float]] = []
            for r in chunk:
                if r["embedding_json"]:
                    try:
                        embeddings.append(json.loads(r["embedding_json"]))
                    except Exception:
                        pass

            block_emb_json: str | None = None
            if embeddings:
                dims = len(embeddings[0])
                mean_emb = [
                    sum(e[i] for e in embeddings) / len(embeddings) for i in range(dims)
                ]
                norm = math.sqrt(sum(x * x for x in mean_emb))
                if norm > 0:
                    mean_emb = [x / norm for x in mean_emb]
                block_emb_json = json.dumps(mean_emb)

            fc_counts: dict[str, int] = {}
            for r in chunk:
                fc = str(r["failure_class"] or "general")
                fc_counts[fc] = fc_counts.get(fc, 0) + 1
            dominant_fc = max(fc_counts, key=lambda k: fc_counts[k]) if fc_counts else "general"

            deltas = [float(r["metric_delta"]) for r in chunk if r["metric_delta"] is not None]
            avg_delta: float | None = sum(deltas) / len(deltas) if deltas else None
            date_from = str(chunk[0]["created_ts"] or "")[:10]
            date_to = str(chunk[-1]["created_ts"] or "")[:10]

            conn.execute(
                "INSERT INTO evolution_memory_blocks "
                "(block_index, entry_ids_json, block_embedding_json, dominant_failure_class, "
                "entry_count, avg_metric_delta, date_from, date_to) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (block_idx, json.dumps(entry_ids), block_emb_json, dominant_fc,
                 len(chunk), avg_delta, date_from, date_to),
            )
            written += 1

    return written


async def fetch_conditioned_evolution_async(
    *,
    diagnosis_text: str,
    get_db: Callable,
    embed_fn: Callable,
    limit: int = 5,
    failure_class: str = "",
) -> list[dict]:
    """Return evolution entries ranked by query-entry cosine similarity.

    Query selection (priority order):
    1. Learned query vector for failure_class if update_count >= 2
    2. Raw diagnosis text embedding (original fallback)

    The learned vector implements G_{t'→t} = softmax(q_t · RMSNorm(e_{t'}))
    where q_t converges toward diagnosis embeddings that produced good proposals.
    """
    diag_embedding, _ = await embed_fn(diagnosis_text)

    query_embedding = diag_embedding
    query_source = "diagnosis"

    if failure_class:
        with get_db() as conn:
            qv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_query_vectors)").fetchall()}
            if "query_vector_json" in qv_cols:
                qv_row = conn.execute(
                    "SELECT query_vector_json, update_count FROM evolution_query_vectors "
                    "WHERE failure_class=?",
                    (failure_class,),
                ).fetchone()
                if qv_row and int(qv_row["update_count"] or 0) >= 2:
                    try:
                        query_embedding = json.loads(qv_row["query_vector_json"])
                        query_source = f"learned(n={int(qv_row['update_count'])})"
                    except Exception:
                        pass

    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        if "embedding_json" not in cols:
            rows = conn.execute(
                "SELECT id,outcome,lesson,failure_class,artifact_family,target_route,metric_delta "
                "FROM evolution_memory ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        rows = conn.execute(
            "SELECT id,outcome,lesson,failure_class,artifact_family,target_route,metric_delta,embedding_json "
            "FROM evolution_memory"
        ).fetchall()

        # Block gating: when ≥2 scored blocks exist, restrict candidates to
        # entries in top ceil(N/2) blocks before per-entry re-ranking.
        allowed_ids: set[int] | None = None
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "evolution_memory_blocks" in tables:
            block_rows = conn.execute(
                "SELECT entry_ids_json, block_embedding_json FROM evolution_memory_blocks"
            ).fetchall()
            scored_blocks = []
            for b in block_rows:
                if b["block_embedding_json"]:
                    try:
                        centroid = json.loads(b["block_embedding_json"])
                        scored_blocks.append((_cosine_similarity(query_embedding, centroid), b))
                    except Exception:
                        pass
            if len(scored_blocks) >= 2:
                scored_blocks.sort(key=lambda x: x[0], reverse=True)
                top_n = max(2, math.ceil(len(scored_blocks) / 2))
                allowed_ids = set()
                for _, b in scored_blocks[:top_n]:
                    try:
                        for eid in json.loads(b["entry_ids_json"]):
                            allowed_ids.add(int(eid))
                    except Exception:
                        pass

    if not rows:
        return []

    # Apply block gate (fall back to all rows if gate empties the set)
    candidates = [r for r in rows if int(r["id"]) in allowed_ids] if allowed_ids else list(rows)
    if not candidates:
        candidates = list(rows)

    # Score candidates and apply softmax so weights match G matrix equation
    raw_scores = []
    for row in candidates:
        emb_json = row["embedding_json"]
        if emb_json:
            try:
                entry_emb = json.loads(emb_json)
                raw_scores.append((_cosine_similarity(query_embedding, entry_emb), row))
            except Exception:
                raw_scores.append((0.0, row))
        else:
            raw_scores.append((0.0, row))

    # Softmax normalisation (temperature=1, stable via max subtraction)
    if raw_scores:
        max_s = max(s for s, _ in raw_scores)
        exp_scores = [(math.exp(s - max_s), row) for s, row in raw_scores]
        total = sum(e for e, _ in exp_scores)
        softmax_scores = [(e / total, row) for e, row in exp_scores]
    else:
        softmax_scores = []

    softmax_scores.sort(key=lambda x: x[0], reverse=True)
    result = [dict(row) for _, row in softmax_scores[:limit]]
    for r in result:
        r["_query_source"] = query_source
    return result


async def embed_evolution_entry_async(
    *,
    proposal_id: int,
    get_db: Callable,
    embed_fn: Callable,
) -> None:
    """Embed lesson text, store in embedding_json, and update learned query vector.

    Called after every /evolve_capture. After storing the embedding,
    triggers an EMA update to the per-failure-class learned query vector
    so future retrievals for this failure class are conditioned on outcomes.
    """
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(evolution_memory)").fetchall()}
        if "embedding_json" not in cols:
            return
        row = conn.execute(
            "SELECT id, lesson, failure_class, artifact_family, metric_delta, outcome "
            "FROM evolution_memory WHERE proposal_id=? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
    if not row:
        return

    text = f"{row['lesson']} failure_class:{row['failure_class']} artifact_family:{row['artifact_family']}"
    embedding, _ = await embed_fn(text)

    with get_db() as conn:
        conn.execute(
            "UPDATE evolution_memory SET embedding_json=? WHERE id=?",
            (json.dumps(embedding), row["id"]),
        )

    # Update learned query vector for this failure class using the diagnosis
    # embedding derived from the lesson text. This closes the loop: after each
    # capture the query vector moves toward patterns that produce good proposals.
    await update_learned_query_vector(
        failure_class=str(row["failure_class"] or "general"),
        diagnosis_embedding=embedding,
        metric_delta=float(row["metric_delta"]) if row["metric_delta"] is not None else None,
        outcome=str(row["outcome"] or ""),
        get_db=get_db,
    )
