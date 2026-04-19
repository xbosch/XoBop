from __future__ import annotations

"""Gmail and Google Calendar skills for XoBop.

Setup (one-time):
  1. Go to console.cloud.google.com → APIs & Services → Credentials
  2. Create OAuth 2.0 Client ID (Desktop App) → download JSON
  3. Save as config/google_credentials.json
  4. Run:  python scripts/google_oauth_setup.py
  5. Restart the bot

Environment variables (optional, these are the defaults):
  GOOGLE_TOKEN_FILE=config/google_token.json
  GOOGLE_CREDENTIALS_FILE=config/google_credentials.json
"""

import base64
import email.mime.text
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

log = logging.getLogger(__name__)

GOOGLE_SKILLS: list[dict[str, Any]] = [
    {
        "name": "gmail_read",
        "description": "List recent Gmail messages. Returns subject, from, date, and snippet for each.",
        "schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of messages to return (default 10, max 25)"},
            },
            "required": [],
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
        "name": "gmail_send",
        "description": "Send an email via Gmail.",
        "schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Plain-text email body"},
            },
            "required": ["to", "subject", "body"],
        },
        "handler_type": "python_fn",
        "risk_level": "high",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 0,
        "visible_in_normal": 1,
    },
    {
        "name": "gmail_search",
        "description": "Search Gmail messages using Gmail query syntax (e.g. 'from:boss is:unread').",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "count": {"type": "integer", "description": "Max results (default 10, max 25)"},
            },
            "required": ["query"],
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
        "name": "calendar_list",
        "description": "List upcoming Google Calendar events.",
        "schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days ahead to look (default 7)"},
            },
            "required": [],
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
        "name": "calendar_create",
        "description": "Create a Google Calendar event.",
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start datetime in ISO 8601 format (e.g. 2026-04-20T10:00:00)"},
                "end": {"type": "string", "description": "End datetime in ISO 8601 format"},
                "description": {"type": "string", "description": "Event description (optional)"},
            },
            "required": ["title", "start", "end"],
        },
        "handler_type": "python_fn",
        "risk_level": "normal",
        "timeout_sec": 30,
        "requires_force": 0,
        "enabled": 1,
        "visible_in_strict": 0,
        "visible_in_normal": 1,
    },
]


def _load_credentials(token_file: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar",
    ]
    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        import pathlib
        pathlib.Path(token_file).write_text(creds.to_json())
    return creds


def _make_gmail_read(svc):
    def gmail_read(args: dict, **_) -> str:
        count = min(int(args.get("count", 10)), 25)
        result = svc.users().messages().list(userId="me", maxResults=count).execute()
        msgs = result.get("messages", [])
        lines = []
        for m in msgs:
            detail = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                                 metadataHeaders=["Subject", "From", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            snippet = detail.get("snippet", "")[:120]
            lines.append(
                f"From: {headers.get('From', '?')}\n"
                f"Date: {headers.get('Date', '?')}\n"
                f"Subject: {headers.get('Subject', '(no subject)')}\n"
                f"Snippet: {snippet}"
            )
        return "\n\n".join(lines) if lines else "No messages found."
    return gmail_read


def _make_gmail_send(svc):
    def gmail_send(args: dict, **_) -> str:
        msg = email.mime.text.MIMEText(args["body"])
        msg["to"] = args["to"]
        msg["subject"] = args["subject"]
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent to {args['to']} — subject: {args['subject']}"
    return gmail_send


def _make_gmail_search(svc):
    def gmail_search(args: dict, **_) -> str:
        count = min(int(args.get("count", 10)), 25)
        result = svc.users().messages().list(userId="me", q=args["query"], maxResults=count).execute()
        msgs = result.get("messages", [])
        if not msgs:
            return f"No results for query: {args['query']}"
        lines = []
        for m in msgs:
            detail = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                                 metadataHeaders=["Subject", "From", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            snippet = detail.get("snippet", "")[:120]
            lines.append(
                f"From: {headers.get('From', '?')}\n"
                f"Date: {headers.get('Date', '?')}\n"
                f"Subject: {headers.get('Subject', '(no subject)')}\n"
                f"Snippet: {snippet}"
            )
        return "\n\n".join(lines)
    return gmail_search


def _make_calendar_list(svc):
    def calendar_list(args: dict, **_) -> str:
        days = int(args.get("days", 7))
        now = datetime.now(timezone.utc)
        time_max = (now + timedelta(days=days)).isoformat()
        result = svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=time_max,
            maxResults=25,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"No events in the next {days} days."
        lines = []
        for ev in events:
            start = ev["start"].get("dateTime", ev["start"].get("date", "?"))
            lines.append(f"{start}  {ev.get('summary', '(no title)')}")
        return "\n".join(lines)
    return calendar_list


def _make_calendar_create(svc):
    def calendar_create(args: dict, **_) -> str:
        body = {
            "summary": args["title"],
            "start": {"dateTime": args["start"], "timeZone": "UTC"},
            "end": {"dateTime": args["end"], "timeZone": "UTC"},
        }
        if args.get("description"):
            body["description"] = args["description"]
        ev = svc.events().insert(calendarId="primary", body=body).execute()
        return f"Event created: {ev.get('summary')} — {ev.get('htmlLink', '')}"
    return calendar_create


def init(token_file: str) -> bool:
    """Load Google credentials and register all skill functions.
    Returns True if successful, False if token file missing or packages not installed."""
    try:
        from googleapiclient.discovery import build
        import pathlib
        if not pathlib.Path(token_file).exists():
            log.info("Google token file not found (%s) — Gmail/Calendar skills inactive", token_file)
            return False
        creds = _load_credentials(token_file)
        gmail_svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        cal_svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        from xobop.skills_runtime import register_python_fn
        register_python_fn("gmail_read", _make_gmail_read(gmail_svc))
        register_python_fn("gmail_send", _make_gmail_send(gmail_svc))
        register_python_fn("gmail_search", _make_gmail_search(gmail_svc))
        register_python_fn("calendar_list", _make_calendar_list(cal_svc))
        register_python_fn("calendar_create", _make_calendar_create(cal_svc))
        log.info("Google skills loaded (Gmail + Calendar)")
        return True
    except ImportError as e:
        log.info("Google API packages not installed (%s) — run: pip install google-api-python-client google-auth-oauthlib", e)
        return False
    except Exception as e:
        log.warning("Google skills init failed: %s", e)
        return False
