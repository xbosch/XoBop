from __future__ import annotations

import os
from pathlib import Path


def system_pulse_impl() -> dict[str, str]:
    pulse: dict[str, str] = {}
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        free_pct = (free / total * 100.0) if total > 0 else 0.0
        pulse["disk_free_pct"] = f"{free_pct:.1f}%"
        pulse["disk_status"] = "ok" if free_pct >= 10.0 else "low"
    except Exception:
        pulse["disk_free_pct"] = "n/a"
        pulse["disk_status"] = "unknown"

    try:
        load1, load5, _ = os.getloadavg()
        cpu_count = max(1, (os.cpu_count() or 1))
        load_norm = load1 / cpu_count
        pulse["load1"] = f"{load1:.2f}"
        pulse["load5"] = f"{load5:.2f}"
        pulse["load_status"] = "ok" if load_norm < 1.0 else "high"
    except Exception:
        pulse["load1"] = "n/a"
        pulse["load5"] = "n/a"
        pulse["load_status"] = "unknown"

    try:
        mem_total_kb = 0
        mem_avail_kb = 0
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail_kb = int(line.split()[1])
        mem_avail_pct = (mem_avail_kb / mem_total_kb * 100.0) if mem_total_kb > 0 else 0.0
        pulse["mem_avail_pct"] = f"{mem_avail_pct:.1f}%"
        pulse["mem_status"] = "ok" if mem_avail_pct >= 10.0 else "low"
    except Exception:
        pulse["mem_avail_pct"] = "n/a"
        pulse["mem_status"] = "unknown"

    temp_c = None
    for p in [
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/hwmon/hwmon0/temp1_input"),
    ]:
        try:
            if p.exists():
                raw = p.read_text().strip()
                v = float(raw)
                temp_c = (v / 1000.0) if v > 1000 else v
                break
        except Exception:
            continue
    if temp_c is None:
        pulse["cpu_temp_c"] = "n/a"
        pulse["temp_status"] = "unknown"
    else:
        pulse["cpu_temp_c"] = f"{temp_c:.1f}"
        pulse["temp_status"] = "ok" if temp_c < 80.0 else "hot"

    return pulse


def health_report_impl(*, get_db, db_file: str, system_pulse, last_openai_latency_ms: float) -> str:
    with get_db() as conn:
        db_size = Path(db_file).resolve().stat().st_size if Path(db_file).exists() else 0
        messages = conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
        memories = conn.execute("SELECT COUNT(*) AS n FROM memory_items WHERE is_deleted=0").fetchone()["n"]
        skills = conn.execute("SELECT COUNT(*) AS n FROM skills WHERE enabled=1").fetchone()["n"]
        cron = conn.execute("SELECT COUNT(*) AS n FROM cron_jobs WHERE enabled=1").fetchone()["n"]
        open_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
        closed_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='done'").fetchone()["n"]
        errors = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status!='ok' AND timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["n"]
        avg_ms = conn.execute(
            "SELECT COALESCE(AVG(duration_ms),0) AS v FROM runs WHERE timestamp >= datetime('now', '-1 hour')"
        ).fetchone()["v"]
        last_cron = conn.execute(
            "SELECT job_name,status,duration_ms FROM cron_runs ORDER BY id DESC LIMIT 3"
        ).fetchall()
    cron_tail = "\n".join([f"  - {r['job_name']}: {'✅' if r['status']=='ok' else '❌'} ({r['duration_ms']}ms)" for r in last_cron]) or "  - none"
    pulse = system_pulse()
    return (
        f"• DB size: {db_size/1024/1024:.2f} MB\n"
        f"• Messages: {messages}\n"
        f"• Memories: {memories}\n"
        f"• Enabled skills: {skills}\n"
        f"• Active cron jobs: {cron}\n"
        f"• Tasks: open={open_tasks} closed={closed_tasks}\n"
        f"• OpenAI latency (last): {last_openai_latency_ms:.1f} ms\n"
        f"• Avg tool latency 1h: {float(avg_ms):.1f} ms\n"
        f"• Errors 1h: {int(errors)}\n"
        f"• Pulse: disk={pulse.get('disk_free_pct','n/a')} ({pulse.get('disk_status','unknown')}), "
        f"mem={pulse.get('mem_avail_pct','n/a')} ({pulse.get('mem_status','unknown')}), "
        f"load1={pulse.get('load1','n/a')} ({pulse.get('load_status','unknown')}), "
        f"temp={pulse.get('cpu_temp_c','n/a')}C ({pulse.get('temp_status','unknown')})\n"
        f"• Last cron runs:\n{cron_tail}"
    )
