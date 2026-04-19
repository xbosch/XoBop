from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple


def store_artifact(*, artifact_dir: Path, prefix: str, content: str, stamp_fn, uuid_fn) -> Path:
    path = artifact_dir / f"{prefix}_{stamp_fn()}_{uuid_fn().hex[:8]}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def shorten_or_artifact(
    *,
    artifact_dir: Path,
    prefix: str,
    text: str,
    artifact_text_threshold: int,
    text_limit: int,
    stamp_fn,
    uuid_fn,
) -> Tuple[str, Optional[str]]:
    if len(text) <= artifact_text_threshold:
        return text[:text_limit], None
    artifact_path = store_artifact(
        artifact_dir=artifact_dir,
        prefix=prefix,
        content=text,
        stamp_fn=stamp_fn,
        uuid_fn=uuid_fn,
    )
    summary = text[:1500] + f"\n[TRUNCATED — full output saved to {artifact_path}]"
    return summary, str(artifact_path)
