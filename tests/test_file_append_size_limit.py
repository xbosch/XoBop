import asyncio
from pathlib import Path

from xobop.skills_runtime import exec_file_append


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _to_thread(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


def test_append_enforces_total_file_size_limit(tmp_path):
    root = tmp_path
    target = root / "a.txt"
    target.write_text("12345", encoding="utf-8")

    out, _code, status, _artifact = asyncio.run(
        exec_file_append(
            skill=type("S", (), {"name": "file_append"})(),
            policies={"write_root": str(root)},
            args={"path": str(target), "content": "67890X"},
            allowed_write_root=root,
            is_under_root=_is_under_root,
            blocked_suffix=lambda _p: None,
            get_policy=lambda _k, _d: 10,
            default_max_write_size=10,
            to_thread=_to_thread,
        )
    )

    assert status == "blocked"
    assert "exceed" in out.lower()
    assert target.read_text(encoding="utf-8") == "12345"
