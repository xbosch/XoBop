from __future__ import annotations

from pathlib import Path

import requests
from bs4 import BeautifulSoup


def read_text_limited(path: Path, max_bytes: int) -> str:
    with path.open("rb") as f:
        raw = f.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="ignore")
    if truncated:
        text += "\n[TRUNCATED]"
    return text


def fetch_text_limited(url: str, *, max_bytes: int, request_timeout: int) -> str:
    with requests.get(url, timeout=request_timeout, stream=True) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                remaining = max_bytes - (total - len(chunk))
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                break
            chunks.append(chunk)
    html = b"".join(chunks).decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    if total > max_bytes:
        text += "\n[TRUNCATED]"
    return text
