import asyncio
from pathlib import Path
from types import SimpleNamespace

from xobop.skills_runtime import exec_file_write


def test_codegen_sandbox_allows_overwrite_without_flag(tmp_path: Path):
    root = tmp_path / "repo"
    target = root / "agent-test" / "codegen-check" / "plot_script.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)

    async def run():
        return await exec_file_write(
            skill=SimpleNamespace(name="write_file"),
            policies={"write_root": str(root)},
            args={"path": str(target), "content": "new"},
            allowed_write_root=root,
            is_under_root=lambda p, r: str(p.resolve()).startswith(str(r.resolve())),
            blocked_suffix=lambda _p: None,
            get_policy=lambda _k, d=None: d,
            default_max_write_size=100000,
            to_thread=_to_thread,
        )

    out, _, status, _ = asyncio.run(run())
    assert status == "ok"
    assert "Written" in out
    assert target.read_text(encoding="utf-8") == "new"
