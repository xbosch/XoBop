from pathlib import Path

from xobop.handlers_runtime import _extract_generated_artifact_paths


def test_extract_generated_zip_path_within_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    z = tmp_path / "xobop_code_latest.zip"
    z.write_text("x", encoding="utf-8")

    out = _extract_generated_artifact_paths("Created zip: xobop_code_latest.zip")
    assert out == [z.resolve()]
