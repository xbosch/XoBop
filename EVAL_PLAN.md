# EVAL_PLAN.md — Sprint L (Freeze + Evidence Harvest)

## Goal
Run a controlled live evaluation window and collect publishable evidence without changing core behavior.

---

## Scope Freeze Rules

During Sprint L:

- Keep artifact families constrained to: `alias`, `retry`
- Do **not** change simulation architecture or pass/fail semantics
- Do **not** modify snapshot/reconciliation logic
- Do **not** add new replay families (email/file/cron)
- Do **not** add arbitrary code patching artifacts

Allowed changes:
- critical bugfixes only
- trial phase switch (`baseline` → `improving`) at planned boundary

---

## Trial Structure

Recommended window: **14 days** minimum (28 better)

- **Phase A: baseline** (7–14 days)
  - promotion disabled by phase policy unless forced emergency
- **Phase B: improving** (7–14 days)
  - promotion enabled per configured policy (`assisted` or `auto_low`)

Record exact dates:

- baseline start: ______
- baseline end: ______
- improving start: ______
- improving end: ______

---

## Primary / Secondary Metrics

### Primary metric (headline)
- **failure rate per 100 commands**

### Secondary metrics
- unknown command rate per 100 commands
- manual override rate
- drift repair frequency
- rollback frequency

---

## Commands to Run

## Trial setup (day 0)

1. Set phase:
```text
/improve_phase baseline
```

2. Confirm state integrity:
```text
/state_check
```

3. Capture initial evidence snapshots:
```text
/improve_weekly_report 2
/improve_funnel 14
/improve_eval_report 14
```

4. If drift exists:
```text
/state_repair
```

---

## Daily routine (all trial days)

Run and archive outputs:

```text
/improve_weekly_report 2
/improve_funnel 14
/improve_eval_report 14
/state_check
```

If `/state_check` reports drift:

```text
/state_repair
```

If rollback event occurs, log it:

```text
/improve_log_rollback <artifact_id> <reason>
```

---

## Midpoint switch (baseline → improving)

On switch day:

```text
/improve_phase improving
```

Then run and archive:

```text
/improve_weekly_report 2
/improve_funnel 14
/improve_eval_report 14
```

---

## Weekly Evidence Bundle

Create dated folders:

```text
eval/
  YYYY-Www/
    weekly_report.txt
    funnel_report.txt
    eval_report.txt
    compare_*.txt
    notes.md
```

Required contents each week:

1. `/improve_weekly_report`
2. `/improve_funnel`
3. `/improve_eval_report 7` (or 14)
4. Representative `/improve_compare <proposal_id> 7|14` outputs
5. Drift/rollback counts from weekly report + notes

---

## Case Study Template (fill per promoted artifact)

- Proposal ID:
- Artifact ID:
- Artifact family: alias / retry
- Problem detected:
- Proposed change:
- Simulation result:
- Deployment timestamp:
- Impact compare summary:
- Drift events after deploy:
- Rollback invoked: yes/no
- Final assessment: positive / neutral / negative

---

## Success Criteria (pre-registered)

Define these **before** final analysis:

1. Primary metric target:
   - failure rate per 100 commands reduced by: ______
2. Unknown command rate reduced by: ______
3. Simulation safety:
   - zero live side effects during simulation runs
4. Reliability bounds:
   - rollback frequency <= ______
   - drift repairs <= ______ per week
5. Quality bounds:
   - no golden-trace regressions in promoted artifacts

---

## End-of-Window Deliverables (Sprint L output)

- Complete weekly bundles for full trial window
- Baseline vs improving phase summary
- Top 2–3 positive case studies
- List of neutral/negative outcomes with reasons
- Draft figure inputs:
  - funnel counts
  - metric time series
  - simulation pass/fail table
  - drift/rollback trend

This is the package used for Sprint M paper drafting.
