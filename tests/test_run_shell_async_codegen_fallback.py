import asyncio
from pathlib import Path

import xobop_core_base as core


def test_run_shell_async_falls_back_to_codegen_sandbox_for_relative_script(tmp_path: Path):
    root = (tmp_path / "repo").resolve()
    sandbox = root / "agent-test" / "codegen-check"
    sandbox.mkdir(parents=True, exist_ok=True)
    script = sandbox / "run_plot.sh"
    script.write_text("#!/usr/bin/env bash\necho hi-from-sandbox\n", encoding="utf-8")

    old_root = core.ALLOWED_WRITE_ROOT
    try:
        core.ALLOWED_WRITE_ROOT = root
        stdout, stderr, rc = asyncio.run(core._run_shell_async("./run_plot.sh", str(root), 10, False))
    finally:
        core.ALLOWED_WRITE_ROOT = old_root

    assert rc == 0
    assert "hi-from-sandbox" in stdout
    assert stderr == ""
