# XoBop V5

Owner-only Telegram execution agent with a governed self-improvement loop, browser automation, and Gmail/Calendar integration.

## What it does

XoBop is a single-user personal agent that runs as a Telegram bot. You talk to it; it executes skills, remembers things, and improves itself based on operational data.

**Core capabilities:**
- Run shell commands, read/write files
- Full browser automation via Playwright — fill forms, click, screenshot, file upload, cookie persistence
- Gmail: read, search, send (after OAuth setup)
- Google Calendar: list and create events (after OAuth setup)
- Semantic memory with embedding retrieval
- Structured task and fact storage (SQLite)
- Cron jobs with skill dispatch

**Self-improvement loop:**
- `/diagnose` — analyses ops signals (failure rates, unknown commands, latency)
- `/improve_propose` — generates a data-backed proposal conditioned on diagnosis embedding
- `/improve_compile <id>` — compiles proposal to a typed artifact (with blocked/error discrimination)
- `/improve_simulate <id>` — simulation gate before any live change
- `/improve_promote <id>` — applies the change with full audit trail
- `/evolve_capture <id>` — embeds the lesson and updates the per-failure-class learned query vector
- `/evolve_recapture_all` — backfill evolution memory embeddings for all past promotions

**Evolution memory (AttnRes-inspired):**

The retrieval system that conditions future proposals on past outcomes implements three layers of the AttnRes governance mixing matrix:

1. **Diagnosis-conditioned retrieval** — cosine similarity between diagnosis embedding and stored entry embeddings, replacing uniform history lookup
2. **Learned query vectors per failure class** — per-failure-class vectors updated via EMA after each `/evolve_capture` (`q_{t+1} = normalize((1-α)·q_t + α·sign·d_t)`); used as query once `update_count ≥ 2`
3. **Block compression** — evolution history partitioned into N=4 temporal blocks; retrieval gates on top-scoring block centroids before per-entry re-ranking; recomputed nightly at 02:00 UTC

## Quick install

```bash
git clone <repo> xobops-dev-clean
cd xobops-dev-clean
./install-xobop.sh
```

The installer creates `.venv`, installs deps, prompts for secrets, and writes `.env`.

Start / stop:
```bash
./scripts/start_dev.sh
./scripts/stop_dev.sh
```

## Manual setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_TOKEN, OPENAI_API_KEY, OWNER_ID
python XoBop.py
```

## Browser automation (one-time)

```bash
source .venv/bin/activate
pip install playwright
playwright install chromium
```

Restart the bot. `browse_page`, `browser_click`, `browser_fill_form`, and `browser_screenshot` activate automatically. Cookies persist across sessions in `config/browser_storage_state.json`.

## Gmail + Calendar (one-time)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Enable Gmail API and Google Calendar API
3. Create OAuth 2.0 Client ID (Desktop App) → download JSON → save as `config/google_credentials.json`
4. Run:
```bash
source .venv/bin/activate
pip install google-api-python-client google-auth-oauthlib
python scripts/google_oauth_setup.py
```
5. Restart the bot. Gmail/Calendar skills activate automatically.

## Telegram bot setup

1. Message **@BotFather** → `/newbot` → copy the token
2. Message **@userinfobot** → copy your numeric ID
3. Set `TELEGRAM_TOKEN` and `OWNER_ID` in `.env`

## Key commands

| Command | What it does |
|---------|-------------|
| `/status` | System status |
| `/health` | Health summary |
| `/diagnose [days]` | Ops diagnosis |
| `/improve_propose [days]` | Generate improvement proposal |
| `/improve_compile <id>` | Compile proposal to artifact |
| `/improve_simulate <id>` | Run simulation gate |
| `/improve_promote <id>` | Promote with risk checks |
| `/improve_reject <id> [reason]` | Reject pending proposal |
| `/improve_compare <id>` | Before/after impact |
| `/improve_set_retry <0-3>` | Reset exec_retry_limit |
| `/evolve_capture <id>` | Embed lesson + update learned query vector |
| `/evolve_recapture_all` | Backfill evolution memory embeddings |
| `/evolve_status` | Report learned query vectors and block compression state |
| `/evolve_blocks_rebuild [N]` | Manually recompute temporal block summaries (default N=4) |
| `/skills` | List available skills |
| `/exec <cmd>` | Run shell command |
| `/remember key: value` | Save a fact |
| `/state_check` | Compare runtime snapshot vs DB |
| `/state_repair` | Repair from snapshot |

## Validation

```bash
source .venv/bin/activate
PYTHONPATH=. pytest -q
python -m py_compile XoBop.py xobop_core.py xobop_core_base.py xobop_core_tail.py xobop/*.py
```

```
125 passed
```

## Architecture

```
XoBop.py → xobop_core.py → xobop_core_base.py + xobop_core_tail.py
                                      ↓
                              xobop/ (domain modules)
                              ├── skills_runtime.py        handler registry + python_fn
                              ├── google_skills.py         Gmail + Calendar
                              ├── browser_skill.py         Playwright automation
                              ├── improve_generate.py      diagnose / propose / compile
                              ├── improve_pipeline_ops.py  simulate / promote / evolve
                              │     ├── fetch_conditioned_evolution_async
                              │     ├── update_learned_query_vector  (EMA, per-failure-class)
                              │     └── recompute_evolution_blocks   (nightly, N=4)
                              ├── migrations.py            additive schema migrations (14)
                              └── ...
```

State is SQLite (`memory.db`). Every skill call, proposal, artifact, simulation run, and promotion is recorded and auditable.

**DB tables added in this session:**
- `evolution_query_vectors` — one learned query vector per failure class, updated after each `/evolve_capture`
- `evolution_memory_blocks` — N=4 temporal block summaries with centroid embeddings, recomputed nightly

## Operational notes

- All changes go through artifact contract + simulation gate before touching live config
- `exec_retry_limit` is capped at 3; reset via `/improve_set_retry`
- Browser cookies persist across sessions in `config/browser_storage_state.json`
- Evolution memory entries are embedded for conditioned retrieval; learned query vectors converge toward diagnosis patterns that produce good proposals
- Block compression limits retrieval noise from early contaminated promotions as history grows
- For architecture details: `docs/ARCHITECTURE.md`, `docs/IMPROVEMENT_PIPELINE.md`
