# XoBop

XoBop is an owner-scoped Telegram copilot for local operations and memory-backed assistance.
It is built to execute practical tasks (shell/file/cron/email workflows) with deterministic routing first and LLM fallback second.
Typical use is: ask in Telegram, XoBop executes with policy gates, persists context in SQLite, and reports concrete outcomes.
The goal is operational reliability over chatty behavior.

## Current status

- ✅ Runtime DB name is `memory.db` (no `_v5` naming in active config/code/docs)
- ✅ Owner-only command surface
- ✅ Runtime prompts stored in DB (`strict` / `normal`)
- ✅ Command routing hardening (`/exec`, direct shell intents, cron list intent)
- ✅ Interactive smoke verification (`scripts/smoke_watch.py`)

---

## Features

- Telegram bot with owner gate (`OWNER_ID`)
- Two runtime modes: `strict`, `normal`
- Runtime prompt control:
  - `/promptshow`
  - `/promptset <mode> <text|reset>`
- Hybrid memory in SQLite:
  - chat transcript
  - semantic memory + vectors
- Skill/tool execution with policy checks
- DB-backed cron scheduler (APScheduler + `cron_jobs`/`cron_runs`)

---

## Requirements

- Python 3.11+
- Telegram bot token
- OpenAI API key

Install:

```bash
cd xobops-test
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
# set TELEGRAM_TOKEN, OPENAI_API_KEY, OWNER_ID (and Gmail vars if email workflows are used)
./run_test.sh
```

> Note: `run_test.sh` is the runtime launcher (legacy name), not a unit-test runner.

---

## Configuration (`.env`)

Required:

- `TELEGRAM_TOKEN`
- `OPENAI_API_KEY`
- `OWNER_ID`

Important defaults/options:

- `DB_FILE=memory.db`
- `MODEL=gpt-4.1-mini`
- `EMBEDDING_MODEL=text-embedding-3-small`
- `DEFAULT_MODE=strict`
- `ALLOW_RAW_SHELL=0`
- `ALLOWED_WRITE_ROOT=/home/xavi/.openclaw/workspace/xobops-test`
- `READ_ROOT=/home/xavi/.openclaw/workspace/xobops-test`
- `GMAIL_EMAIL=...` (optional, needed for email workflows)
- `GMAIL_APP_PASSWORD=...` (optional, needed for email workflows)

### `.env.example` (canonical template)

```env
TELEGRAM_TOKEN=your_telegram_bot_token
OPENAI_API_KEY=your_openai_key
OWNER_ID=5410368885
GMAIL_EMAIL=your_gmail_address@gmail.com
GMAIL_APP_PASSWORD=your_16_char_google_app_password
MODEL=gpt-4.1-mini
EMBEDDING_MODEL=text-embedding-3-small
DB_FILE=memory.db
DEFAULT_MODE=strict
ARTIFACT_DIR=artifacts
ALLOWED_WRITE_ROOT=/home/xavi/.openclaw/workspace/xobops-test
READ_ROOT=/home/xavi/.openclaw/workspace/xobops-test
ALLOW_RAW_SHELL=0
EMBEDDING_FALLBACK=1
FILE_MEMORY_ROOT=/home/xavi/.openclaw/workspace/xobops-test
LONG_MEMORY_FILE=/home/xavi/.openclaw/workspace/xobops-test/MEMORY.md
DAILY_MEMORY_DIR=/home/xavi/.openclaw/workspace/xobops-test/memory
```

---

## Project structure

```text
xobops-test/
├── XoBop.py                 # Main entrypoint/app wiring
├── xobop/
│   ├── router.py            # Unified action parser/classifier
│   ├── state.py             # Runtime state store (pending actions, last results)
│   ├── db.py                # SQLite session helper (WAL/busy_timeout/commit-rollback)
│   ├── intents.py           # Intent extraction utilities (email fields, refs)
│   ├── workflows.py         # Generic workflow engine (pending action progression)
│   ├── emailer.py           # SMTP email send helper
│   ├── capabilities_fs.py   # Filesystem capabilities (e.g., count entries)
│   ├── capabilities_shell.py# Shell execution capability with unified result contract
│   ├── services.py          # Intent service adapters (download/shell/git flows)
│   └── handlers_runtime.py  # Transport-agnostic message/media processing routines
├── run_test.sh              # Launcher
├── .env                     # Secrets (ignored)
├── .env.example             # Template
├── .gitignore               # Ignore rules for secrets/runtime artifacts
├── requirements.txt
├── memory.db                # Runtime DB (ignored)
├── memory.db-shm/.wal       # SQLite runtime files (ignored)
├── scripts/
│   ├── smoke_watch.py       # Interactive smoke verifier
│   └── smoke_test.sh        # Wrapper for smoke_watch
├── artifacts/               # Runtime artifacts (ignored)
├── backups/                 # DB backups (ignored)
└── memory/                  # File memory notes (ignored)
```

---

## Database structure (`memory.db`)

Primary tables used by runtime:

- `conversations` — chat transcript (`chat_id`, `role`, `content`, `timestamp`)
- `memory_items` — semantic memory entries (`text`, `mem_type`, `importance`, `source`, `pinned`, `is_deleted`)
- `memory_vectors` — embedding vectors linked to `memory_items`
- `settings` — runtime settings (e.g., active mode)
- `runtime_prompts` — prompt text per mode (`strict`, `normal`)
- `global_policies` — safety/limits config
- `skills` — enabled skill registry + metadata
- `skill_policies` — per-skill policy overrides
- `runs` — tool execution logs
- `tasks` — open/closed task tracking
- `cron_jobs` — scheduled jobs (`name`, `cron_expr`, `skill_type`, `skill_args`, state)
- `cron_runs` — cron execution history
- `rate_events` — rate-limit accounting buckets

Quick schema inspection:

```bash
python3 - <<'PY'
import sqlite3
con=sqlite3.connect('./memory.db')
for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(row[0])
con.close()
PY
```

### Migration note

- Current runtime expects `DB_FILE` schema created/managed by `_init_db` at startup.
- Fresh DBs are auto-initialized.
- Legacy DB migration from older versions is not fully automated/documented yet; test on a copy first.

---

## Architecture layers

1. **Transport layer** (`XoBop.py` handlers)
   - Telegram updates, commands, media intake, error handling
2. **Routing layer** (`xobop/router.py`)
   - unified intent/action classification
3. **Workflow layer** (`xobop/workflows.py`, `xobop/state.py`)
   - pending actions, continuation, status tracking
4. **Capability layer** (`xobop/capabilities_*.py`, `xobop/emailer.py`)
   - shell/files/email executable operations
5. **Agent layer** (`_run_agent` in `XoBop.py`)
   - Memory read path: semantic retrieval from `memory_items` + `memory_vectors` via `_build_memory_block`
   - Injects relevant memories into system context before LLM call
   - LLM + tool loop when structured routes do not apply
6. **Persistence/scheduling layer** (`memory.db`, APScheduler)
   - durable state, cron jobs, run history

## Runtime flow

1. Telegram update arrives
2. Owner check
3. Routing:
   - command handlers (`/status`, `/cron*`, `/prompt*`, etc.)
   - structured router/actions (deterministic)
   - fallback to `_run_agent()` for general chat/tool loop
4. In `_run_agent()`, memory read path runs first:
   - query embeddings + similarity retrieval from `memory_items`/`memory_vectors`
   - relevant memories are injected into prompt context
5. Persistence in `memory.db`
6. Scheduler runs `cron_jobs`, writes `cron_runs`

---

## Commands

Core examples:

- `/status`
- `/mode strict|normal`
- `/promptshow`
- `/promptset strict|normal <text|reset>`
- `/exec <command>`
- `/cronadd ...`
- `/cronlist`
- `/cronrun <name>`
- `/cronruns <name> [limit]`

Additional supported commands include memory/task/skill controls such as:

- `/remember`, `/delfact`, `/facts`
- `/addtask`, `/donetask`, `/tasks`
- `/memories`, `/memadd`, `/memdel`
- `/skills`, `/skillon`, `/skilloff`
- `/clearctx`, `/help`

For complete and current command list, use `/help` in Telegram.

---

## Smoke testing

Run:

```bash
cd /home/xavi/.openclaw/workspace/xobops-test
./scripts/smoke_test.sh
```

What it does:
- launches interactive verifier (`smoke_watch.py`)
- prompts you to send command + free-text checks in Telegram
- validates assistant replies via `memory.db`

Interpretation:
- **PASS**: all prompted checks matched expected output tokens
- **FAIL**: script prints failing step + missing token or timeout reason

---

## Backup safety (critical)

Do **not** use `cp memory.db ...` while bot is running.

Use SQLite-safe backup methods (`sqlite3 .backup` or Python `sqlite3.Connection.backup`) to avoid corruption.

---

## Architecture diagram

```mermaid
%%{init: {'flowchart': {'useMaxWidth': true, 'nodeSpacing': 22, 'rankSpacing': 28}, 'theme': 'default'}}%%
flowchart LR
    U[Owner user] --> T[Telegram update]
    T --> R{Route type}

    R --> C[Command handlers]
    R --> D[Deterministic intents]
    R --> A[_run_agent]

    C --> DB[(memory.db)]
    D --> S[Skill executor]
    A --> MR[Memory retrieval\n(memory_items + vectors)]
    MR --> A
    A --> L[LLM]
    L --> S
    S --> DB

    J[APScheduler] --> CJ[cron_jobs]
    CJ --> S
    S --> CR[cron_runs]
    CR --> DB

    C --> O[Reply]
    D --> O
    A --> O
```
