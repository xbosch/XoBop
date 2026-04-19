from pathlib import Path

from xobop.capabilities_shell import _resolve_script_path


def test_resolve_script_path_uses_fallback_dir(tmp_path: Path):
    cwd = (tmp_path / "repo").resolve()
    fallback = cwd / "agent-test" / "codegen-check"
    fallback.mkdir(parents=True, exist_ok=True)
    script = fallback / "run_plot.sh"
    script.write_text("echo hi", encoding="utf-8")

    out = _resolve_script_path("./run_plot.sh --flag", cwd=cwd, fallback_dirs=[fallback])
    assert str(script) in out
    assert "--flag" in out


def test_resolve_script_path_raises_when_missing(tmp_path: Path):
    cwd = (tmp_path / "repo").resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        _resolve_script_path("./run.sh", cwd=cwd, fallback_dirs=[cwd / "agent-test" / "codegen-check"])
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "Script not found" in str(exc)
