from pathlib import Path

from xobop.improve_generate import build_improve_priorities_block


def test_missing_file_returns_empty_string(tmp_path: Path):
    assert build_improve_priorities_block(tmp_path / "does_not_exist.md") == ""


def test_present_file_is_read_and_stripped(tmp_path: Path):
    p = tmp_path / "IMPROVE_PRIORITIES.md"
    p.write_text("  \nFocus on latency this week.\n  ", encoding="utf-8")
    assert build_improve_priorities_block(p) == "Focus on latency this week."


def test_empty_file_returns_empty_string(tmp_path: Path):
    p = tmp_path / "IMPROVE_PRIORITIES.md"
    p.write_text("   \n  ", encoding="utf-8")
    assert build_improve_priorities_block(p) == ""


def test_long_file_is_truncated(tmp_path: Path):
    p = tmp_path / "IMPROVE_PRIORITIES.md"
    p.write_text("x" * 5000, encoding="utf-8")
    assert len(build_improve_priorities_block(p)) == 2000
