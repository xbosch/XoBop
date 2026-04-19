from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_checkup_sync_impl(*, system_pulse) -> str:
    checks = []
    code_path = Path(__file__).resolve().parent.parent / "XoBop.py"
    venv_python = code_path.parent / ".venv" / "bin" / "python"
    checks.append(("venv_python", venv_python.exists()))
    req = Path("requirements.txt").resolve()
    checks.append(("requirements_txt", req.exists()))
    for key in ["TELEGRAM_TOKEN", "OPENAI_API_KEY", "OWNER_ID", "DB_FILE"]:
        checks.append((f"env_{key}", bool(os.getenv(key))))

    dep_ok = True
    dep_note = "ok"
    if venv_python.exists() and req.exists():
        try:
            proc = subprocess.run(
                [str(venv_python), "-m", "pip", "check"],
                text=True,
                capture_output=True,
                timeout=20,
            )
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                dep_note = (out or "No broken requirements found")[:180]
            else:
                dep_ok = False
                dep_note = (out or f"pip check failed (exit {proc.returncode})")[:180]
        except Exception as exc:
            dep_ok = False
            dep_note = str(exc)[:180]
    checks.append(("pip_check", dep_ok))

    pulse = system_pulse()
    checks.append(("pulse_disk", pulse.get("disk_status") in {"ok", "unknown"}))
    checks.append(("pulse_mem", pulse.get("mem_status") in {"ok", "unknown"}))
    checks.append(("pulse_load", pulse.get("load_status") in {"ok", "unknown"}))
    checks.append(("pulse_temp", pulse.get("temp_status") in {"ok", "unknown"}))

    bad = [name for name, ok in checks if not ok]
    git_rev = "unknown"
    try:
        git_rev = subprocess.check_output(["git", "-C", str(code_path.parent), "rev-parse", "--short", "HEAD"], text=True).strip() or "unknown"
    except Exception:
        pass

    lines = [f"• {name}: {'✅' if ok else '❌'}" for name, ok in checks]
    lines.append(f"• code_path: {code_path}")
    lines.append(f"• git_rev: {git_rev}")
    lines.append(f"• venv_python_path: {venv_python}")
    lines.append(f"• venv_python_resolved: {venv_python.resolve()}")
    lines.append(
        f"• pulse: disk={pulse.get('disk_free_pct','n/a')} ({pulse.get('disk_status','unknown')}), "
        f"mem={pulse.get('mem_avail_pct','n/a')} ({pulse.get('mem_status','unknown')}), "
        f"load1={pulse.get('load1','n/a')} ({pulse.get('load_status','unknown')}), "
        f"temp={pulse.get('cpu_temp_c','n/a')}C ({pulse.get('temp_status','unknown')})"
    )
    lines.append(f"• pip_check_note: {dep_note}")
    lines.append(f"RESULT: {'PASS' if not bad else f'FAIL ({', '.join(bad)})'}")
    return "\n".join(lines)


def generate_system_map_sync_impl(*, cwd: str) -> str:
    root = Path(cwd)
    out = root / "memory" / "SYSTEM_MAP.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    files = sorted([str(p.relative_to(root)) for p in (root / "xobop").glob("*.py")])
    body = [
        "# SYSTEM_MAP.md",
        "",
        "Auto-generated architecture snapshot.",
        "",
        "## Modules",
        *[f"- `{f}`" for f in files],
        "",
        "## Entry",
        "- `XoBop.py`",
    ]
    out.write_text("\n".join(body), encoding="utf-8")
    return str(out)
