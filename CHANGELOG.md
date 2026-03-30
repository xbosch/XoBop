# CHANGELOG

## v0.9.0 - 2026-03-30

### Highlights
- Modular architecture progression (router/state/workflows/capabilities/services/handlers runtime).
- Deterministic routing hardening for commands, cron listing, shell execution paths.
- Email + attachment workflow generalized with pending-action state.
- Semantic memory read path documented/instrumented; lexical fallback improved to FTS5.
- Safe online SQLite backup support with timestamp + git-hash naming.
- State hydration across restarts + artifact watcher notifications.
- Quiet-hours queue + digest flush.
- Prompt proposal lifecycle (`/promptproposal`, `/promptapprove`, `/promptreject`).
- New operational commands: `/checkup`, `/plan`, `/go`.
- Weekly prompt review and system map refresh cron jobs.

### Reliability/Safety
- Shell preflight destructive pattern hard-blocks.
- Enriched `/health` report (DB size, cron tail, task stats, latency).
- Expanded skill audit data (`owner_id`, `cmd_input`, `exit_code`).

### Notes
- Runtime DB migrations are additive and applied at startup via `_init_db()`.
- Default cron set includes: `db-backup-6h`, `prompt-review-weekly`, `checkup-monthly`, `system-map-weekly`.
