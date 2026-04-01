# Testing & Validation

## Baseline suite

```bash
source .venv/bin/activate
pytest -q
```

## Compile checks

```bash
python -m py_compile XoBop.py xobop_core.py xobop_core_base.py xobop_core_tail.py xobop/*.py
```

## Eval suite

```bash
python scripts/eval_suite.py
```

## Minimum merge gate

Any structural/runtime change should pass all three:
1. `py_compile`
2. `pytest`
3. `eval_suite`

## Post-refactor smoke

Recommended manual Telegram checks after major refactors:
- `/status`
- `/help`
- `/cronlist`
- `/improve_list`
- `/panicstatus`
