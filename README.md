# XoBop

Owner-only Telegram operations copilot with safety gates, bounded self-improvement, and reproducible runtime behavior.

## What it is

XoBop is designed for:
- deterministic operations
- strict safety around shell and writes
- measurable improvement loops
- reproducible state and migration-safe DB evolution

## Current Architecture (modularized)

Core entrypoint remains `XoBop.py` (wiring), with extracted modules in `xobop/`:

- `config.py` — env/settings loader
- `schema.py` — core DB schema creation
- `migrations.py` — versioned schema migrations (`schema_migrations` table)
- `db_init.py` — DB init orchestration
- `cron_bootstrap.py` — default cron job seeding
- `bootstrap.py` — Telegram app build + handler registration
- `handlers_runtime.py` — runtime message/command handling
- `router.py` — intent classification
- `output_guardrails.py` — machine-output normalization + anti-fabrication guardrails
- `agent.py` — core LLM tool-loop orchestration
- `capabilities_shell.py` / `shell_facade.py` — shell policy + live/sim execution facade
- `simulation/` — replay controller, manifests, temp DB sandbox

## Safety model

- Owner-only command surface (`OWNER_ID`)
- Panic-lock mode for execution freeze
- Shell hard blocks for destructive patterns
- Write/read root boundaries
- Force-gated risky actions
- PASS-downgrade guardrails when proof/checksum evidence is missing

## Commands

### Core
- `/help`
- `/status`
- `/health`
- `/checkup`
- `/state_check`
- `/state_repair`
- `/mode [strict|normal|overseer]`
- `/exec <command>`

### Improvement lifecycle
- `/diagnose [days]`
- `/improve_propose [days]`
- `/improve_list`
- `/improve_compile <proposal_id>`
- `/improve_artifacts [proposal_id]`
- `/improve_simulate <proposal_id>`
- `/improve_runs [proposal_id]`
- `/improve_compare <proposal_id> [window_days]`
- `/improve_policy [manual|assisted|auto_low]`
- `/improve_promote <proposal_id> [force <reason>]`
- `/improve_autopromote`

### Cron
- `/cronadd name | schedule | skill_type | description | args_json`
- `/cronrm <name>`
- `/cronlist`
- `/cronon <name>`
- `/cronoff <name>`
- `/cronrun <name>`
- `/cronruns <name> [limit]`

## Environment

Required:
- `TELEGRAM_TOKEN`
- `OPENAI_API_KEY`
- `OWNER_ID`

Important defaults:
- `DB_FILE=memory.db`
- `DEFAULT_MODE=strict`
- `MODEL=gpt-4.1-mini`
- `EMBEDDING_MODEL=text-embedding-3-small`
- `ALLOW_RAW_SHELL=0`
- `ALLOWED_WRITE_ROOT=<repo_root>`
- `READ_ROOT=<repo_root or broader read root>`

## Setup

```bash
cd /home/xavi/.openclaw/workspace/xobops-dev-clean
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
./run_xobop_test.sh
```

## Tests

Smoke/unit coverage (pytest):
- `tests/test_output_guardrails.py`
- `tests/test_migrations.py`
- `tests/test_db_init.py`
- `tests/test_command_registry.py`

Run:
```bash
pytest -q
```

## Validation helpers

```bash
python3 -m py_compile XoBop.py xobop/*.py xobop/simulation/*.py scripts/eval_suite.py
.venv/bin/python scripts/eval_suite.py
.venv/bin/python scripts/build_signals.py
```

## Notes

- During evaluation windows, keep scope controlled and changes reversible.
- Prefer evidence-backed behavior over optimistic outputs.
- Keep safety boundaries strict; tune throughput policies gradually.
