from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from typing import Callable


def _norm(s: str, max_len: int = 220) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t[:max_len]


def _cluster_signature(query: str, result: str, status: str) -> str:
    q = _norm(query, 180)
    r = _norm((result or "").splitlines()[0] if result else "", 120)
    raw = f"{status}|{q}|{r}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _quality_ok(query: str, result: str) -> bool:
    q = _norm(query, 240)
    r = _norm(result, 240)
    # Ignore low-information / synthetic noise.
    if len(q.split()) < 4:
        return False
    if "synthetic failure" in q or "synthetic failure" in r:
        return False
    if "test trigger" in q:
        return False
    return True


def maybe_generate_auto_proposals(
    *,
    get_db: Callable[[], sqlite3.Connection],
    enabled: bool,
    dry_run: bool,
    threshold: int,
    max_per_day: int,
    dedup_days: int,
) -> dict:
    if not enabled:
        return {"enabled": False, "created": 0, "considered": 0}

    with get_db() as conn:
        conn.row_factory = sqlite3.Row

        # daily cap for auto-proposals
        today_auto = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM improvement_proposals WHERE created_ts >= date('now') AND metric_target LIKE 'AUTO_SIG:%'"
            ).fetchone()["n"]
        )
        if today_auto >= max_per_day:
            return {"enabled": True, "created": 0, "considered": 0, "capped": True}

        rows = conn.execute(
            """
            SELECT status,user_query,result
            FROM runs
            WHERE timestamp >= datetime('now','-1 day')
              AND status IN ('error','blocked')
            """
        ).fetchall()

        clusters: dict[str, dict] = {}
        for r in rows:
            query = str(r["user_query"] or "")
            result = str(r["result"] or "")
            if not _quality_ok(query, result):
                continue
            sig = _cluster_signature(query, result, str(r["status"] or ""))
            c = clusters.get(sig)
            if not c:
                c = {
                    "sig": sig,
                    "status": str(r["status"] or "error"),
                    "count": 0,
                    "query": query,
                    "result": result,
                }
                clusters[sig] = c
            c["count"] += 1

        candidates = [c for c in clusters.values() if int(c["count"]) >= max(1, int(threshold))]
        candidates.sort(key=lambda c: int(c["count"]), reverse=True)

        created = 0
        considered = 0
        created_ids: list[int] = []
        safe_for_autopromote = True
        followups = 0
        for c in candidates:
            considered += 1
            if created + today_auto >= max_per_day:
                break

            sig = c["sig"]
            existing = conn.execute(
                "SELECT COUNT(*) AS n FROM improvement_proposals WHERE metric_target LIKE ? AND created_ts >= datetime('now', ?)",
                (f"AUTO_SIG:{sig}%", f"-{int(dedup_days)} day"),
            ).fetchone()["n"]
            if int(existing) > 0:
                # trend-followup: if failures doubled threshold, create one daily follow-up proposal.
                if int(c["count"]) >= max(2 * int(threshold), int(threshold) + 3):
                    fu_existing = conn.execute(
                        "SELECT COUNT(*) AS n FROM improvement_proposals WHERE metric_target LIKE ? AND created_ts >= date('now')",
                        (f"AUTO_FOLLOWUP_SIG:{sig}%",),
                    ).fetchone()["n"]
                    if int(fu_existing) == 0 and (created + today_auto) < max_per_day and not dry_run:
                        title = f"Auto follow-up: persistent failure cluster ({c['count']}x)"
                        metric = f"AUTO_FOLLOWUP_SIG:{sig};COUNT:{c['count']};SOURCE:auto_failure_cluster"
                        cur = conn.execute(
                            """
                            INSERT INTO improvement_proposals
                            (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
                            VALUES ('proposed','medium',?,?,?,?,?,?)
                            """,
                            (
                                title,
                                "Failure cluster remains active after prior auto-proposal(s).",
                                _norm(c["result"], 220) or "unknown",
                                "Escalate analysis depth and enforce deterministic mitigation with stricter regression checks.",
                                "Reduce persistent recurrence and close long-tail instability.",
                                metric,
                            ),
                        )
                        try:
                            created_ids.append(int(cur.lastrowid))
                        except Exception:
                            pass
                        # follow-up proposals are medium-risk and must never auto-promote.
                        safe_for_autopromote = False
                        followups += 1
                        created += 1
                continue

            title = f"Auto: reduce recurring {c['status']} cluster ({c['count']}x)"
            problem = f"Recurring {c['status']} failures detected in last 24h."
            cause = _norm(c["result"], 220) or "unknown"
            change = "Introduce deterministic guard/fallback for this failure signature and add regression test."
            benefit = f"Lower repeated {c['status']} rate for this cluster."
            metric = f"AUTO_SIG:{sig};COUNT:{c['count']};SOURCE:auto_failure_cluster"

            # guard autopromote safety: blocked-policy clusters need human review.
            if c['status'] != 'error':
                safe_for_autopromote = False

            if dry_run:
                continue

            cur = conn.execute(
                """
                INSERT INTO improvement_proposals
                (status,risk_level,title,problem_statement,suspected_cause,proposed_change,expected_benefit,metric_target)
                VALUES ('proposed','low',?,?,?,?,?,?)
                """,
                (title, problem, cause, change, benefit, metric),
            )
            created += 1
            try:
                created_ids.append(int(cur.lastrowid))
            except Exception:
                pass

        return {
            "enabled": True,
            "created": created,
            "considered": considered,
            "dry_run": dry_run,
            "created_ids": created_ids,
            "safe_for_autopromote": safe_for_autopromote,
            "followups": followups,
        }


def auto_improve_flags(get_setting: Callable[[str, str], str] | None = None) -> tuple[bool, bool, int, int, int]:
    def _cfg(key: str, default: str) -> str:
        if get_setting is not None:
            try:
                v = get_setting(key, default)
                if v is not None:
                    return str(v)
            except Exception:
                pass
        return str(os.getenv(key, default) or default)

    enabled = _cfg("AUTO_IMPROVE_ENABLED", "0").strip() == "1"
    dry_run = _cfg("AUTO_IMPROVE_DRY_RUN", "1").strip() == "1"
    threshold = int(_cfg("AUTO_IMPROVE_MIN_CLUSTER", "5").strip())
    max_per_day = int(_cfg("AUTO_IMPROVE_MAX_PER_DAY", "5").strip())
    dedup_days = int(_cfg("AUTO_IMPROVE_DEDUP_DAYS", "7").strip())
    return enabled, dry_run, threshold, max_per_day, dedup_days
