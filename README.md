# XoBop

Personal Telegram copilot with memory, skills, and cron automation.

## Features

- Owner-only Telegram bot control
- Two runtime modes (`strict`, `normal`) with DB-backed runtime prompts
- Long-term memory in SQLite (`memory_v5.db`) + embeddings
- Skills/tool execution with policy gates
- Cron jobs stored in DB and executed by APScheduler
- Gmail helper workflow scripts (noise filtering + sender triage)

## Requirements

- Python 3.11+
- Telegram bot token
- OpenAI API key

## Quick install

```bash
cd xobops-test
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
# edit .env and set TELEGRAM_TOKEN + OPENAI_API_KEY + OWNER_ID
./run_test.sh
```

## Configuration

Main file: `.env`

Required:
- `TELEGRAM_TOKEN`
- `OPENAI_API_KEY`
- `OWNER_ID`

Important optional:
- `MODEL` (default: `gpt-4.1-mini`)
- `EMBEDDING_MODEL` (default: `text-embedding-3-small`)
- `DEFAULT_MODE` (`strict` or `normal`)
- `ALLOW_RAW_SHELL` (`0` recommended)

## Telegram command examples

- `/status`
- `/mode strict`
- `/mode normal`
- `/promptshow`
- `/promptset normal You are concise and practical.`
- `/cronlist`
- `/cronrun <job_name>`

## Notes

- Keep `.env`, database files, and backups out of git.
- Cron command parsing is strict for JSON payloads; keep `/cronadd ... | {json}` on one line.
- For production, run under systemd/tmux and rotate backups.

## Project layout

- `XoBop.py` — main bot runtime
- `run_test.sh` — local launcher
- `.env.example` — config template
- `memory_v5.db` — runtime DB (ignored)
- `artifacts/`, `backups/`, `memory/` — runtime state (ignored)

## Optional static website structure

If you publish a simple static artist site, this structure works well:

```text
/
├── index.html            Home
├── about.html            Artist information
├── contact.html          Contact and inquiries
├── css/
│   └── style.css         Global styles
├── gallery/
│   ├── index.html        Gallery overview
│   ├── olives.html       Olives series
│   ├── faces.html        Faces series
│   └── symbols.html      Symbols series
└── images/
    ├── olives/
    ├── faces/
    └── symbols/
```
