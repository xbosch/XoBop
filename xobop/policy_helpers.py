from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse


def is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def domain_allowed(url: str, get_policy: Callable[[str, str], str]) -> bool:
    raw = get_policy("allowed_http_domains", "").strip()
    if not raw:
        return True
    allowed = [d.strip().lower() for d in raw.split(",") if d.strip()]
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)


def blocked_suffix(
    path: Path,
    *,
    get_policy: Callable[[str, str], str],
    default_blocked_suffixes: str,
    allowed_write_root: Path,
) -> Optional[str]:
    blocked_raw = get_policy("blocked_write_suffixes", default_blocked_suffixes)
    blocked = [s.strip().lower() for s in blocked_raw.split(",") if s.strip()]
    lower_name = path.name.lower()

    try:
        sandbox_root = (allowed_write_root / "agent-test").resolve()
        in_sandbox = is_under_root(path.resolve(), sandbox_root)
    except Exception:
        in_sandbox = False

    for suffix in blocked:
        if lower_name.endswith(suffix):
            if in_sandbox and suffix in {".py", ".sh"}:
                continue
            return suffix
    return None
