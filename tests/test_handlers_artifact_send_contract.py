import asyncio
from pathlib import Path
from types import SimpleNamespace

from xobop.handlers_runtime import _maybe_send_generated_artifacts


class _Msg:
    def __init__(self):
        self.sent = []

    async def reply_document(self, *, document, filename, caption):
        self.sent.append({"filename": filename, "caption": caption, "size": len(document.read())})


def test_send_here_fallback_picks_newest_zip_when_result_mentions_zip(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old_zip = tmp_path / "old.zip"
    new_zip = tmp_path / "new.zip"
    import os, time

    old_zip.write_bytes(b"old")
    new_zip.write_bytes(b"new")
    now = time.time()
    os.utime(old_zip, (now - 20, now - 20))
    os.utime(new_zip, (now, now))

    msg = _Msg()
    update = SimpleNamespace(message=msg)
    log = SimpleNamespace(warning=lambda *a, **k: None)

    asyncio.run(
        _maybe_send_generated_artifacts(
            update,
            "Created archive successfully: xobop_code_latest.zip",
            log,
            user_text="send it here",
        )
    )

    assert len(msg.sent) == 1
    assert msg.sent[0]["filename"] == "new.zip"


def test_no_send_when_no_artifact_path_and_user_did_not_request_send(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "xobop_code_latest.zip").write_bytes(b"zip")

    msg = _Msg()
    update = SimpleNamespace(message=msg)
    log = SimpleNamespace(warning=lambda *a, **k: None)

    asyncio.run(
        _maybe_send_generated_artifacts(
            update,
            "Archive created: xobop_code_latest.zip",
            log,
            user_text="ok thanks",
        )
    )

    assert msg.sent == []
