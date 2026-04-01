# Improvement Pipeline

## Stages

1. **Diagnose** (`/diagnose`) -> operational signals
2. **Propose** (`/improve_propose`) -> `improvement_proposals`
3. **Compile** (`/improve_compile`) -> typed `change_artifacts`
4. **Simulate** (`/improve_simulate`) -> compile/eval/replay checks
5. **Promote** (`/improve_promote`) -> policy + risk-gated apply
6. **Impact** (`/improve_compare`) -> before/after telemetry deltas

## Artifact families

Controlled by setting:
- `artifact_families_allowed`

Current families:
- `alias`
- `retry`
- `lineage`
- `prompt`

## Contract enforcement

Each artifact type/target pair has expected:
- `apply_points`
- `invariants`
- `contract_version`
- `contract_hash`

Simulation and promotion reject artifacts with mismatched contracts.

## Prompt artifact risk

Prompt artifacts have global blast radius. Current policy:
- `prompt_change:runtime_prompt` requires **force** for promotion.
- prompt validation includes replay-oriented checks.

## Operational commands

- `/improve_list`, `/improve_artifacts`, `/improve_runs`
- `/improve_policy ...`
- `/improve_funnel`, `/improve_weekly_report`, `/improve_eval_report`
- `/improve_log_rollback <artifact_id> <reason>`
