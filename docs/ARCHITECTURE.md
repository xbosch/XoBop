# Architecture

## Runtime entry chain

1. `XoBop.py` (thin launcher + compatibility bridge)
2. `xobop_core.py` (aggregator)
3. `xobop_core_base.py` + `xobop_core_tail.py`
4. Domain modules in `xobop/`

This layout keeps compatibility (`python XoBop.py`) while enforcing a strict max-lines-per-file policy.

## Main subsystems

- **State & DB:** `schema.py`, `migrations.py`, `db_init.py`, `runtime_state.py`, `runtime_snapshot.py`
- **Telegram app lifecycle:** `bootstrap.py`, `app_lifecycle.py`, `app_handlers.py`
- **Execution pipeline:** `skills_runtime.py`, `capabilities_shell.py`, `shell_facade.py`, `runtime_ops.py`
- **Command domains:** `commands_*.py` modules
- **Improve engine:** `improve.py`, `improve_generate.py`, `improve_pipeline_ops.py`
- **Attachment/document stack:** `attachments_runtime.py`, `attachments.py`, `io_text.py`, `artifacts_io.py`

## Design principles

- Owner-only operational surface
- Policy-first execution (deny by default on risky paths)
- Deterministic state with snapshot+repair
- Measurable improvement loop with explicit gates
- Refactorable modular boundaries
