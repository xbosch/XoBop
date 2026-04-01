# XoBop V5 (xobop-dev)

Owner-only Telegram operations copilot with strict safety controls, durable state, and a governed self-improvement loop.

## TL;DR

- **Interface:** Telegram bot, owner-scoped command surface
- **Runtime state:** SQLite (`memory.db`) + deterministic snapshot/state-repair paths
- **Execution:** skill-based (read/write/http/shell/agent), with policy gates
- **Safety:** panic-lock, shell/write boundaries, rate limits, force-gated risk
- **Improvement:** proposal → artifact → simulation → promotion → impact comparison

---

## Architecture (current)

After refactor, the legacy monolith entrypoint is now thin:

- `XoBop.py` — compatibility launcher (thin)
- `xobop_core.py` — thin aggregator
- `xobop_core_base.py` — core runtime segment A
- `xobop_core_tail.py` — core runtime segment B

And domain modules under `xobop/` continue to absorb logic:

- **Bootstrap/runtime wiring**
  - `bootstrap.py`, `db_init.py`, `schema.py`, `migrations.py`, `cron_bootstrap.py`
- **Routing/handlers**
  - `handlers_runtime.py`, `router.py`, `app_handlers.py`, `app_lifecycle.py`
- **Command domains**
  - `commands_admin.py`, `commands_improve.py`, `commands_memory.py`, `commands_safety.py`, `commands_skills.py`, `commands_cron.py`, `commands_prompt.py`
- **Execution + policy**
  - `skills_runtime.py`, `capabilities_shell.py`, `shell_facade.py`, `shell_policy.py`, `policy_helpers.py`, `runtime_ops.py`
- **Improve pipeline**
  - `improve.py`, `improve_generate.py`, `improve_pipeline_ops.py`, `runtime_state.py`, `runtime_snapshot.py`
- **Attachment/document processing**
  - `attachments.py`, `attachments_runtime.py`, `artifacts_io.py`, `io_text.py`
- **Math/text helpers**
  - `text_math.py`, `memory_math.py`, `mode_utils.py`, `commands_core.py`, `commands_ops.py`, `ops_logging.py`, `selfmods.py`

For deeper docs, see:
- `docs/ARCHITECTURE.md`
- `docs/IMPROVEMENT_PIPELINE.md`
- `docs/OPERATIONS.md`
- `docs/TESTING.md`

---

## Safety model

- Owner-only command execution (`OWNER_ID`)
- Panic-lock for execution freeze (`/paniclock`, `/panicunlock`, `/panicstatus`)
- Shell hard-block and typed policy checks
- Read/write root enforcement
- Rate limits (tool and shell buckets)
- Force-required paths for high-risk changes (including prompt artifacts)
- Snapshot/state drift detection and repair (`/state_check`, `/state_repair`)

---

## Key command families

### Core ops
- `/help`, `/status`, `/health`, `/checkup`
- `/state_check`, `/state_repair`
- `/mode [strict|normal|overseer]`
- `/exec <command>`

### Memory/tasks
- `/remember key: value`, `/delfact key`, `/facts`
- `/memories [limit]`, `/memadd text`, `/memdel id`
- `/addtask [priority:] desc`, `/donetask id`, `/tasks`

### Improve lifecycle
- `/diagnose [days]`
- `/improve_propose [days]`, `/improve_list`
- `/improve_compile <proposal_id>`, `/improve_artifacts [proposal_id]`
- `/improve_simulate <proposal_id>`, `/improve_runs [proposal_id]`
- `/improve_compare <proposal_id> [window_days]`
- `/improve_policy ...`, `/improve_promote ...`, `/improve_autopromote`

### Cron
- `/cronadd name | schedule | skill_type | description | args_json`
- `/cronrm <name>`, `/cronlist`, `/cronon <name>`, `/cronoff <name>`
- `/cronrun <name>`, `/cronruns <name> [limit]`

### Prompt/quiet-hours
- `/promptshow`, `/promptset <mode> <text>`
- `/promptproposal`, `/promptapprove`, `/promptreject`
- `/quiethours HH:MM-HH:MM`, `/quietoff`, `/quietflush`

---

## Environment

Required:
- `TELEGRAM_TOKEN`
- `OPENAI_API_KEY`
- `OWNER_ID`

Important defaults:
- `DB_FILE=memory.db`
- `DEFAULT_MODE=strict`
- `MODEL=gpt-4.1-mini` (or configured runtime model)
- `EMBEDDING_MODEL=text-embedding-3-small`
- `ALLOW_RAW_SHELL=0`

See `.env.example` for complete variable list.

---

## Setup

```bash
cd /home/xavi/.openclaw/workspace/xobops-dev-clean
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
```

Run bot:

```bash
source .venv/bin/activate
python XoBop.py
```

---

## Validation

```bash
source .venv/bin/activate
python -m py_compile XoBop.py xobop_core.py xobop_core_base.py xobop_core_tail.py xobop/*.py
pytest -q
python scripts/eval_suite.py
```

---

## Refactor policy (important)

- Keep behavior stable while moving code.
- Prefer domain extraction over adding wrappers in-place.
- No single runtime file should exceed **2000 lines**.
- Every refactor commit should pass:
  - `py_compile`
  - `pytest`
  - `eval_suite` (when touched areas overlap improve/runtime policy)
