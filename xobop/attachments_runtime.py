from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Callable

from .attachments import extract_docx_text, extract_xlsx_text, summarize_zip


async def analyze_attachment(*, chat_id: int, path: str, user_hint: str, state, client, model: str) -> str:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        state.last_attachment_diag_by_chat[int(chat_id)] = {"path": str(p), "status": "missing", "extractor": "none"}
        return ""
    size = p.stat().st_size
    suffix = p.suffix.lower()
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    diag: dict[str, Any] = {"path": str(p), "mime": mime, "size": int(size), "extractor": "metadata", "status": "ok"}

    def _ret(msg: str, *, extractor: str, status: str = "ok") -> str:
        diag["extractor"] = extractor
        diag["status"] = status
        diag["preview_chars"] = len(msg or "")
        state.last_attachment_diag_by_chat[int(chat_id)] = dict(diag)
        return msg

    if mime.startswith("image/"):
        if size > 8 * 1024 * 1024:
            return _ret(f"Attachment is image ({mime}, {size} bytes). Too large for inline analysis; use path directly.", extractor="image_metadata", status="degraded")
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        prompt = (
            "Analyze this uploaded image for task execution. "
            "Return concise structured notes: what it shows, key text (OCR), and actionable fields. "
            "If it's a map/screenshot/document, extract source, destination, values, and units when visible."
        )
        if user_hint:
            prompt += f" User request context: {user_hint[:300]}"
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a precise document/image parser."},
                    {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]},
                ],
                temperature=0.0,
                max_tokens=700,
            )
            return _ret((resp.choices[0].message.content or "").strip(), extractor="image_vision")
        except Exception as exc:
            return _ret(f"Attachment is image ({mime}, {size} bytes). Vision analysis unavailable: {str(exc)[:120]}", extractor="image_vision", status="degraded")

    text_like = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".log", ".ini", ".toml", ".xml", ".html", ".py", ".js", ".ts", ".sh"}
    if suffix in text_like or mime.startswith("text/"):
        raw = p.read_bytes()[:120000]
        decoded = raw.decode("utf-8", errors="ignore").strip()
        if decoded:
            return _ret(f"Attachment text preview ({mime}, {size} bytes):\n{decoded[:2500]}", extractor="text_preview")
        return _ret(f"Attachment metadata: mime={mime}, size={size} bytes; no decodable UTF-8 text preview.", extractor="text_metadata", status="degraded")

    if suffix == ".pdf" or mime == "application/pdf":
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(str(p))
            chunks = []
            for page in reader.pages[:8]:
                t = (page.extract_text() or "").strip()
                if t:
                    chunks.append(t)
            text = "\n\n".join(chunks).strip()
            if text:
                return _ret(f"PDF text extract ({size} bytes):\n{text[:3000]}", extractor="pdf_text")
        except Exception:
            pass
        return _ret(f"Attachment is PDF ({size} bytes). Text extraction unavailable in current runtime; use conversion/OCR pipeline.", extractor="pdf_metadata", status="degraded")

    if suffix == ".docx" or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            text = extract_docx_text(p, max_chars=3500)
            if text:
                return _ret(f"DOCX text extract ({size} bytes):\n{text}", extractor="docx_text")
            return _ret(f"DOCX detected ({size} bytes) but no readable text found.", extractor="docx_metadata", status="degraded")
        except Exception as exc:
            return _ret(f"DOCX extraction failed: {str(exc)[:120]}", extractor="docx_error", status="error")

    if suffix in {".xlsx", ".xlsc"} or mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        try:
            text = extract_xlsx_text(p, max_chars=3500)
            if text:
                return _ret(f"XLSX table preview ({size} bytes):\n{text}", extractor="xlsx_text")
            return _ret(f"XLSX detected ({size} bytes) but no readable cells found.", extractor="xlsx_metadata", status="degraded")
        except Exception as exc:
            return _ret(f"XLSX extraction failed: {str(exc)[:120]}", extractor="xlsx_error", status="error")

    if suffix == ".zip" or mime == "application/zip":
        try:
            listing = summarize_zip(p)
            return _ret(f"ZIP archive listing ({size} bytes):\n{listing}", extractor="zip_listing")
        except Exception as exc:
            return _ret(f"ZIP analysis failed: {str(exc)[:120]}", extractor="zip_error", status="error")

    return _ret(f"Attachment metadata: name={p.name}, mime={mime}, size={size} bytes. Binary/non-text document; direct content extraction may require converter/OCR.", extractor="binary_metadata", status="degraded")
