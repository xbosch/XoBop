import asyncio

import pytest

from xobop.media_skills import MEDIA_SKILLS, _make_send_document, _make_send_photo
from xobop.policy_helpers import is_under_root


def _run(coro):
    return asyncio.run(coro)


def test_media_skills_declares_both_skills():
    names = {s["name"] for s in MEDIA_SKILLS}
    assert names == {"send_photo", "send_document"}
    for s in MEDIA_SKILLS:
        assert s["handler_type"] == "python_fn"
        assert "path" in s["schema"]["required"]


def test_send_photo_rejects_path_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\x00")

    sent = []

    async def fake_send(path, caption):
        sent.append((path, caption))

    fn = _make_send_photo(send_photo_fn=fake_send, read_root_fn=lambda: root, is_under_root=is_under_root)
    with pytest.raises(PermissionError):
        _run(fn({"path": str(outside)}))
    assert sent == []


def test_send_photo_sends_file_under_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    img = root / "photo.jpg"
    img.write_bytes(b"\x00")

    sent = []

    async def fake_send(path, caption):
        sent.append((path, caption))

    fn = _make_send_photo(send_photo_fn=fake_send, read_root_fn=lambda: root, is_under_root=is_under_root)
    result = _run(fn({"path": str(img), "caption": "hi"}))
    assert "photo.jpg" in result
    assert sent == [(img.resolve(), "hi")]


def test_send_document_rejects_missing_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    missing = root / "nope.pdf"

    async def fake_send(path, caption):
        raise AssertionError("should not be called")

    fn = _make_send_document(send_document_fn=fake_send, read_root_fn=lambda: root, is_under_root=is_under_root)
    with pytest.raises(FileNotFoundError):
        _run(fn({"path": str(missing)}))


def test_send_document_rejects_oversized_file(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    big = root / "big.bin"
    big.write_bytes(b"\x00")

    import xobop.media_skills as media_skills
    monkeypatch.setattr(media_skills, "_MAX_MEDIA_BYTES", 0)

    async def fake_send(path, caption):
        raise AssertionError("should not be called")

    fn = _make_send_document(send_document_fn=fake_send, read_root_fn=lambda: root, is_under_root=is_under_root)
    with pytest.raises(ValueError):
        _run(fn({"path": str(big)}))
