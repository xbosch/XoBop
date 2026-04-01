# Operations Runbook

## Startup

```bash
source .venv/bin/activate
python XoBop.py
```

## Health checks

Telegram commands:
- `/status`
- `/health`
- `/checkup`
- `/state_check`

If drift is detected:
- `/state_repair`

## Safety controls

- `/panicstatus`
- `/paniclock arm`
- `/paniclock confirm <token>`
- `/panicunlock <code>`

When panic-lock is ON, execution paths are blocked except safe/status paths.

## Cron maintenance

- `/cronlist`
- `/cronruns <name> [limit]`
- `/cronrun <name>`

## Attachment diagnostics

- `/attachdiag`

Use this after uploads if extraction quality is unclear.

## Export sanitized source

```bash
scripts/export_sanitized.sh
```

This excludes DB/secrets/artifacts/state cache paths.
