from __future__ import annotations

"""Outbound media skills for XoBop — send files back to the owner over Telegram.

Registered skills:
  send_photo     — send an image file as a Telegram photo
  send_document  — send any file as a Telegram document (images, PDFs, spreadsheets, ...)

Both require dependencies injected by core at extended-skill init time (send_fn),
since the Telegram bot instance only exists after the Application is built.
"""

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_MAX_MEDIA_BYTES = 20 * 1024 * 1024  # Telegram bot API cap for uploads by file path

MEDIA_SKILLS: list[dict[str, Any]] = [
    {
        "name": "send_photo",
        "description": (
            "Send an image file to the owner in this chat as a Telegram photo. "
            "path must be an absolute path under the read root (e.g. a file previously "
            "downloaded, an inbox upload, or a screenshot artifact). Use this whenever "
            "the user asks to see/view/show an image rather than just describing it."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to an image file"},
                "caption": {"type": "string", "description": "Optional caption shown with the photo"},
            },
            "required": ["path"],
        },
        "handler_type": "python_fn",
        "risk_level": "low",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
    },
    {
        "name": "send_document",
        "description": (
            "Send any file to the owner in this chat as a Telegram document (images, PDFs, "
            "spreadsheets, logs, etc). path must be an absolute path under the read root."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "caption": {"type": "string", "description": "Optional caption shown with the file"},
            },
            "required": ["path"],
        },
        "handler_type": "python_fn",
        "risk_level": "low",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
    },
]


def _resolve_under_root(raw_path: str, read_root: Path, is_under_root: Callable[[Path, Path], bool]) -> Path:
    path = Path(raw_path).expanduser().resolve()
    root = Path(read_root).resolve()
    if not is_under_root(path, root):
        raise PermissionError(f"Path outside read root: {root}")
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    size = path.stat().st_size
    if size > _MAX_MEDIA_BYTES:
        raise ValueError(f"File too large for Telegram upload: {size:,} bytes (max {_MAX_MEDIA_BYTES:,})")
    return path


def _make_send_photo(*, send_photo_fn: Callable[[Path, str], Awaitable[None]], read_root_fn: Callable[[], Path], is_under_root: Callable[[Path, Path], bool]):
    async def send_photo(args: dict, **_) -> str:
        path = _resolve_under_root(str(args["path"]), read_root_fn(), is_under_root)
        caption = str(args.get("caption", "") or "")[:1024]
        await send_photo_fn(path, caption)
        return f"Sent photo to owner: {path.name}"
    return send_photo


def _make_send_document(*, send_document_fn: Callable[[Path, str], Awaitable[None]], read_root_fn: Callable[[], Path], is_under_root: Callable[[Path, Path], bool]):
    async def send_document(args: dict, **_) -> str:
        path = _resolve_under_root(str(args["path"]), read_root_fn(), is_under_root)
        caption = str(args.get("caption", "") or "")[:1024]
        await send_document_fn(path, caption)
        return f"Sent document to owner: {path.name}"
    return send_document


def init(*, send_photo_fn, send_document_fn, read_root_fn, is_under_root) -> bool:
    """Register the send_photo / send_document skill functions.

    send_photo_fn / send_document_fn: async (Path, caption:str) -> None, provided by
    core (they know how to reach the live Telegram Application/bot and OWNER_ID).
    """
    try:
        from xobop.skills_runtime import register_python_fn
        register_python_fn("send_photo", _make_send_photo(
            send_photo_fn=send_photo_fn, read_root_fn=read_root_fn, is_under_root=is_under_root,
        ))
        register_python_fn("send_document", _make_send_document(
            send_document_fn=send_document_fn, read_root_fn=read_root_fn, is_under_root=is_under_root,
        ))
        log.info("Media skills loaded (send_photo, send_document)")
        return True
    except Exception as e:
        log.warning("Media skills init failed: %s", e)
        return False
