from __future__ import annotations

"""Playwright-based browser skills for XoBop.

Skills registered:
  browse_page          — navigate to URL, return full rendered page text (cookies persisted)
  browser_click        — navigate + click an element (CSS selector or text=...)
  browser_fill_form    — fill form fields, optional file upload, optional submit
  browser_screenshot   — take a screenshot and save as artifact
"""

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STORAGE_STATE_FILE = os.getenv("BROWSER_STORAGE_STATE", "config/browser_storage_state.json")
_MAX_TEXT = 32_000  # chars returned from browser


BROWSER_SKILLS: list[dict[str, Any]] = [
    {
        "name": "browse_page",
        "description": (
            "Fetch a URL using a full browser (JavaScript rendered). "
            "Returns visible page text. Cookies are persisted across calls so logged-in sessions survive restarts."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            },
            "required": ["url"],
        },
        "handler_type": "python_fn",
        "risk_level": "low",
        "timeout_sec": 45,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
        "policies": {"max_bytes": "32000"},
    },
    {
        "name": "browser_click",
        "description": (
            "Navigate to a URL and click an element. "
            "selector can be a CSS selector or 'text=Visible label'. "
            "Returns the resulting page text after click. Cookies persisted."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "selector": {"type": "string", "description": "CSS selector or text=Label to click"},
            },
            "required": ["url", "selector"],
        },
        "handler_type": "python_fn",
        "risk_level": "normal",
        "timeout_sec": 45,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 0,
        "visible_in_normal": 1,
    },
    {
        "name": "browser_fill_form",
        "description": (
            "Navigate to a URL, fill form fields, optionally upload a file, and optionally submit. "
            "fields maps CSS selectors to text values. "
            "file_upload maps a file input CSS selector to an absolute file path. "
            "submit_selector is optional; clicks that element after filling."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
                "fields": {
                    "type": "object",
                    "description": "CSS selector → text value",
                    "additionalProperties": {"type": "string"},
                },
                "file_upload": {
                    "type": "object",
                    "description": "CSS selector of file input → absolute path of file to upload",
                    "additionalProperties": {"type": "string"},
                },
                "submit_selector": {
                    "type": "string",
                    "description": "CSS selector or text=... of submit button (optional)",
                },
            },
            "required": ["url", "fields"],
        },
        "handler_type": "python_fn",
        "risk_level": "high",
        "timeout_sec": 60,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 0,
        "visible_in_normal": 1,
    },
    {
        "name": "browser_screenshot",
        "description": (
            "Navigate to a URL and take a full-page screenshot. "
            "Returns the saved file path as an artifact. "
            "Useful for visual inspection of any webpage."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to screenshot"},
                "full_page": {"type": "boolean", "description": "Capture full scrollable page (default true)"},
            },
            "required": ["url"],
        },
        "handler_type": "python_fn",
        "risk_level": "low",
        "timeout_sec": 45,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 1,
        "visible_in_normal": 1,
    },
]


async def _open_context(p):
    """Launch Chromium with persistent cookies if storage state file exists."""
    browser = await p.chromium.launch(headless=True)
    sf = Path(_STORAGE_STATE_FILE)
    if sf.exists():
        context = await browser.new_context(storage_state=str(sf))
    else:
        context = await browser.new_context()
    return browser, context


async def _save_context(context) -> None:
    """Persist cookies and local storage for next session."""
    sf = Path(_STORAGE_STATE_FILE)
    sf.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(sf))


async def _browse_page(args: dict, policies: dict, **_) -> str:
    from playwright.async_api import async_playwright
    url = str(args["url"]).strip()
    max_chars = int(policies.get("max_bytes", _MAX_TEXT))
    async with async_playwright() as p:
        browser, context = await _open_context(p)
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            text = await page.inner_text("body")
            await _save_context(context)
            return text[:max_chars]
        finally:
            await browser.close()


async def _browser_click(args: dict, **_) -> str:
    from playwright.async_api import async_playwright
    url = str(args["url"]).strip()
    selector = str(args["selector"]).strip()
    async with async_playwright() as p:
        browser, context = await _open_context(p)
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await page.click(selector, timeout=10_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            text = await page.inner_text("body")
            await _save_context(context)
            return text[:_MAX_TEXT]
        finally:
            await browser.close()


async def _browser_fill_form(args: dict, **_) -> str:
    from playwright.async_api import async_playwright
    url = str(args["url"]).strip()
    fields: dict = args.get("fields", {})
    file_upload: dict = args.get("file_upload", {})
    submit_selector: str | None = args.get("submit_selector")
    async with async_playwright() as p:
        browser, context = await _open_context(p)
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            for selector, value in fields.items():
                await page.fill(selector, str(value))
            for selector, file_path in file_upload.items():
                await page.set_input_files(selector, file_path)
            if submit_selector:
                await page.click(submit_selector, timeout=10_000)
                await page.wait_for_load_state("networkidle", timeout=15_000)
                title = await page.title()
                await _save_context(context)
                body = (await page.inner_text("body"))[:_MAX_TEXT]
                parts = [f"Form submitted. Result page: {title}"]
                if file_upload:
                    parts.append(f"Files uploaded: {list(file_upload.values())}")
                parts.append(body)
                return "\n\n".join(parts)
            parts = [f"Form filled ({len(fields)} field(s))"]
            if file_upload:
                parts.append(f"Files staged for upload: {list(file_upload.values())}")
            parts.append("No submit_selector — form not submitted.")
            await _save_context(context)
            return " ".join(parts)
        finally:
            await browser.close()


async def _browser_screenshot(args: dict, **_) -> tuple:
    from playwright.async_api import async_playwright
    import time
    url = str(args["url"]).strip()
    full_page = bool(args.get("full_page", True))
    artifact_dir = Path(os.getenv("ARTIFACT_DIR", "artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    out_path = artifact_dir / f"screenshot_{ts}.png"
    async with async_playwright() as p:
        browser, context = await _open_context(p)
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            title = await page.title()
            await page.screenshot(path=str(out_path), full_page=full_page)
            await _save_context(context)
        finally:
            await browser.close()
    return (f"Screenshot saved: {out_path}\nPage title: {title}", None, "ok", str(out_path))


def init() -> bool:
    """Register Playwright skill functions. Returns True if Playwright is available."""
    try:
        import playwright  # noqa: F401
        from xobop.skills_runtime import register_python_fn
        register_python_fn("browse_page", _browse_page)
        register_python_fn("browser_click", _browser_click)
        register_python_fn("browser_fill_form", _browser_fill_form)
        register_python_fn("browser_screenshot", _browser_screenshot)
        log.info("Playwright browser skills loaded (browse_page, browser_click, browser_fill_form, browser_screenshot)")
        return True
    except ImportError:
        log.info("Playwright not installed — browse_page stays on http_get")
        return False
    except Exception as e:
        log.warning("Browser skills init failed: %s", e)
        return False
