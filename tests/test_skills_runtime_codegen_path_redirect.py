import asyncio
from pathlib import Path
from types import SimpleNamespace

from xobop.skills_runtime import exec_file_write


def test_bare_py_write_redirects_to_codegen_sandbox(tmp_path: Path):
    root = (tmp_path / "repo").resolve()
    root.mkdir(parents=True, exist_ok=True)

    async def _to_thread(fn, *a, **k):
        return fn(*a, **k)

    async def run():
        return await exec_file_write(
            skill=SimpleNamespace(name="write_file"),
            policies={"write_root": str(root)},
            args={"path": "plot_script.py", "content": "print(1)"},
            allowed_write_root=root,
            is_under_root=lambda p, r: str(p.resolve()).startswith(str(r.resolve())),
            blocked_suffix=lambda _p: None,
            get_policy=lambda _k, d=None: d,
            default_max_write_size=100000,
            to_thread=_to_thread,
        )

    out, _, status, _ = asyncio.run(run())
    assert status == "ok"
    expected = root / "agent-test" / "codegen-check" / "plot_script.py"
    assert expected.exists()
    assert str(expected) in out
