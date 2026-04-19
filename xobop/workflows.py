from __future__ import annotations

import os
import re
import time
from enum import Enum
from typing import Optional

from .intents import (
    extract_email_body,
    extract_recipient_email,
    is_email_intent,
    is_email_status_intent,
    wants_attachment,
)


class EmailWorkflowState(str, Enum):
    AWAITING_ATTACHMENT = "AWAITING_ATTACHMENT"
    DRAFT_READY = "DRAFT_READY"
    CONFIRMED = "CONFIRMED"


def _persist_workflow_state(state, chat_id: int, key: str, value) -> None:
    saver = getattr(state, "workflow_state_saver", None)
    if callable(saver):
        try:
            saver(chat_id, key, value)
        except Exception:
            pass


def _set_email_state(state, chat_id: int, wf_state: EmailWorkflowState) -> None:
    if hasattr(state, "email_workflow_state_by_chat"):
        state.email_workflow_state_by_chat[chat_id] = wf_state.value
        _persist_workflow_state(state, chat_id, "email_workflow_state", wf_state.value)


def _email_draft_ttl_seconds() -> int:
    try:
        return max(300, int(os.getenv("EMAIL_DRAFT_TTL_SECONDS", "21600") or 21600))
    except Exception:
        return 21600


def _stamp_draft(payload: dict) -> dict:
    p = dict(payload)
    p["draft_ts"] = int(time.time())
    return p


def _is_draft_stale(payload: dict) -> bool:
    try:
        ts = int(payload.get("draft_ts", 0) or 0)
    except Exception:
        ts = 0
    if ts <= 0:
        return False
    return (int(time.time()) - ts) > _email_draft_ttl_seconds()


def _is_placeholder_email(addr: str) -> bool:
    a = (addr or "").strip().lower()
    return a.endswith("@example.com") or a.endswith("@example.org") or a.endswith("@example.net")


def _is_allowed_recipient(email: str, state) -> bool:
    a = (email or "").strip().lower()
    if not a:
        return False

    get_fact = getattr(state, "fact_getter", None)
    get_all = getattr(state, "all_facts_getter", None) or getattr(state, "fact_items_getter", None)

    # Backward-compatible default for test/minimal state objects.
    if not callable(get_fact) and not callable(get_all):
        return True

    if callable(get_fact):
        try:
            default = (get_fact("default_recipient_email") or "").strip().lower()
            if default and a == default:
                return True
        except Exception:
            pass

    if callable(get_all):
        try:
            for k, v in (get_all() or {}).items():
                kk = str(k).strip().lower()
                vv = str(v).strip().lower()
                if kk.startswith("email_alias_") and vv == a:
                    return True
        except Exception:
            pass

    return False


def _resolve_recipient_email(text: str, state) -> Optional[str]:
    direct = extract_recipient_email(text)
    if direct:
        if _is_placeholder_email(direct):
            return direct
        if not _is_allowed_recipient(direct, state):
            return None
        return direct
    resolver = getattr(state, "email_contact_resolver", None)
    if callable(resolver):
        try:
            return resolver(text)
        except Exception:
            return None
    return None


def _is_recipient_allowlisted(addr: str, text: str, state) -> bool:
    a = (addr or "").strip().lower()
    if not a:
        return False

    resolver = getattr(state, "email_contact_resolver", None)
    if callable(resolver):
        try:
            if (resolver(text) or "").strip().lower() == a:
                return True
        except Exception:
            pass

    return _is_allowed_recipient(a, state)


def _recipient_hint(state) -> str:
    get_fact = getattr(state, "fact_getter", None)
    if callable(get_fact):
        try:
            hint = (get_fact("default_recipient_email") or "").strip()
            if hint:
                return hint
        except Exception:
            pass
    return "recipient"


def _is_confirmation(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"yes", "yes!", "y", "ok", "okay", "confirm", "do it", "send it", "send it now", "now"}


def _extract_subject_body_inline(text: str) -> tuple[Optional[str], Optional[str]]:
    t = text or ""
    subject: Optional[str] = None
    body: Optional[str] = None

    # subject: ... [body: ...]
    sm = re.search(r"\bsubject\s*:\s*(.+?)(?=\s+\bbody\s*:|$)", t, re.I | re.S)
    if sm:
        subject = sm.group(1).strip()

    # body: ... (to end)
    bm = re.search(r"\bbody\s*:\s*(.+)$", t, re.I | re.S)
    if bm:
        body = bm.group(1).strip()

    return (subject or None), (body or None)


def _is_email_followup(text: str) -> bool:
    t = (text or "").strip().lower()
    if t.startswith(("subject:", "body:", "attach ", "attach", "send it now", "send now", "send email", "send mail")):
        return True
    if "what is the current draft" in t or "show draft" in t:
        return True
    # recipient references are valid follow-up only when explicit email wording exists
    return ("to " in t) and ("email" in t or "mail" in t)


def handle_email_text(text: str, chat_id: int, state) -> dict:
    """Return workflow decision dict.

    Keys:
      handled: bool
      reply: Optional[str]
      send_now: bool
      to, subject, body, attachment
    """
    raw = (text or "").strip()
    lower = raw.lower()

    # Safety gate: large multi-line prompts should never trigger email side-effects
    # unless the first line is explicitly email workflow related.
    if "\n" in raw:
        first = raw.splitlines()[0].strip().lower()
        explicit_first_line = (
            is_email_intent(first)
            or first.startswith("send email")
            or first.startswith("subject:")
            or first.startswith("body:")
            or first.startswith("attach")
            or first in {"send it now", "send now", "yes", "yes!"}
        )
        if not explicit_first_line:
            return {"handled": False}

    payload_store = getattr(state, "last_email_payload_by_chat", {}) or {}
    status_store = getattr(state, "last_email_status_by_chat", {}) or {}
    attachment_store = getattr(state, "last_attachment_by_chat", {}) or {}

    if lower in {"discard email", "cancel email", "discard draft", "cancel draft", "clear email draft"}:
        had = bool(payload_store.get(chat_id) or state.pending_action_by_chat.get(chat_id))
        payload_store.pop(chat_id, None)
        state.pending_action_by_chat.pop(chat_id, None)
        _persist_workflow_state(state, chat_id, "last_email_payload", None)
        _persist_workflow_state(state, chat_id, "pending_action", None)
        if hasattr(state, "email_workflow_state_by_chat"):
            state.email_workflow_state_by_chat.pop(chat_id, None)
            _persist_workflow_state(state, chat_id, "email_workflow_state", None)
        msg = "Email draft discarded." if had else "No email draft to discard or already discarded."
        return {"handled": True, "reply": msg, "send_now": False}

    if is_email_status_intent(text) or "what did you" in lower:
        payload = payload_store.get(chat_id)
        status = status_store.get(chat_id)
        if payload:
            attach = payload.get("attachment") or "none"
            return {
                "handled": True,
                "reply": f"Last email: to={payload.get('to')} subject={payload.get('subject')} attachment={attach}\nBody: {payload.get('body')}\nStatus: {status or 'unknown'}",
                "send_now": False,
            }
        return {
            "handled": True,
            "reply": status or "No email sent yet in this chat. Start a draft with: send email to <address>.",
            "send_now": False,
        }

    draft = dict(payload_store.get(chat_id) or {})
    if draft and _is_draft_stale(draft):
        payload_store.pop(chat_id, None)
        state.pending_action_by_chat.pop(chat_id, None)
        _persist_workflow_state(state, chat_id, "last_email_payload", None)
        _persist_workflow_state(state, chat_id, "pending_action", None)
        if hasattr(state, "email_workflow_state_by_chat"):
            state.email_workflow_state_by_chat.pop(chat_id, None)
            _persist_workflow_state(state, chat_id, "email_workflow_state", None)
        draft = {}

    # Follow-up send detection should stay email-explicit; avoid generic "send it here" hijacks.
    followup_send = ("send" in lower) and ("email" in lower or "mail" in lower)

    # Draft field updates in separate messages.
    if draft and lower.startswith("subject:"):
        draft["subject"] = text.split(":", 1)[1].strip() if ":" in text else draft.get("subject", "Message from Xavi")
        if hasattr(state, "last_email_payload_by_chat"):
            stamped = _stamp_draft(draft)
            state.last_email_payload_by_chat[chat_id] = stamped
            _persist_workflow_state(state, chat_id, "last_email_payload", stamped)
        return {"handled": True, "reply": f"Subject updated: {draft['subject']}", "send_now": False}

    if draft and lower.startswith("body:"):
        draft["body"] = text.split(":", 1)[1].strip() if ":" in text else draft.get("body", "")
        if hasattr(state, "last_email_payload_by_chat"):
            stamped = _stamp_draft(draft)
            state.last_email_payload_by_chat[chat_id] = stamped
            _persist_workflow_state(state, chat_id, "last_email_payload", stamped)
        return {"handled": True, "reply": "Body updated.", "send_now": False}

    # If a draft exists, treat only explicit email follow-ups as email workflow commands.
    if draft and _is_email_followup(text):
        # Safety-first: only explicit send-now confirmations trigger immediate send.
        send_now_confirmed = ("send now" in lower) or ("send it now" in lower)

        attachment = draft.get("attachment") or attachment_store.get(chat_id)
        resolved_to = _resolve_recipient_email(text, state)
        is_send_email_phrase = ("send" in lower) and ("email" in lower or "mail" in lower)
        explicit_target_reference = is_send_email_phrase and (
            (" to " in lower)
            or bool(re.search(r"\b(him|her|them)\b", lower))
        )
        to_candidate = resolved_to or ("" if explicit_target_reference else (draft.get("to") or ""))

        fallback_body = extract_email_body(text)
        body_candidate = draft.get("body") or fallback_body

        if send_now_confirmed:
            payload = {
                "handled": True,
                "reply": None,
                "send_now": True,
                "to": to_candidate,
                "subject": draft.get("subject") or "Message from Xavi",
                "body": body_candidate,
                "attachment": attachment,
            }
            if not payload["to"]:
                return {"handled": True, "reply": f"Please provide recipient email (e.g. {_recipient_hint(state)}).", "send_now": False}
            if _is_placeholder_email(payload["to"]):
                return {"handled": True, "reply": "Recipient looks like a placeholder address. Please provide a real email.", "send_now": False}
            validate_now = bool(extract_recipient_email(text) or resolved_to)
            if validate_now and (not _is_recipient_allowlisted(payload["to"], text, state)):
                return {"handled": True, "reply": "Recipient is not allowlisted. Set it via /remember email_alias_* or default_recipient_email, or include explicit address in your message.", "send_now": False}
            if not payload["body"]:
                return {"handled": True, "reply": "What should I say in the email body?", "send_now": False}
            if hasattr(state, "last_email_payload_by_chat"):
                persisted = {
                    "to": payload["to"],
                    "subject": payload["subject"],
                    "body": payload["body"],
                    "attachment": payload["attachment"],
                }
                stamped = _stamp_draft(persisted)
                state.last_email_payload_by_chat[chat_id] = stamped
                _persist_workflow_state(state, chat_id, "last_email_payload", stamped)
            _set_email_state(state, chat_id, EmailWorkflowState.CONFIRMED)
            return payload

        # non-confirm follow-up: refresh draft, require explicit send-now later
        refreshed = {
            "to": to_candidate or draft.get("to") or "",
            "subject": draft.get("subject") or "Message from Xavi",
            "body": body_candidate,
            "attachment": attachment,
        }
        if hasattr(state, "last_email_payload_by_chat"):
            stamped = _stamp_draft(refreshed)
            state.last_email_payload_by_chat[chat_id] = stamped
            _persist_workflow_state(state, chat_id, "last_email_payload", stamped)
        _set_email_state(state, chat_id, EmailWorkflowState.DRAFT_READY)
        if not refreshed["to"]:
            return {"handled": True, "reply": f"Please provide recipient email (e.g. {_recipient_hint(state)}).", "send_now": False}
        if _is_placeholder_email(refreshed["to"]):
            return {"handled": True, "reply": "Recipient looks like a placeholder address. Please provide a real email.", "send_now": False}
        validate_now = bool(extract_recipient_email(text) or resolved_to)
        if validate_now and (not _is_recipient_allowlisted(refreshed["to"], text, state)):
            return {"handled": True, "reply": "Recipient is not allowlisted. Set it via /remember email_alias_* or default_recipient_email, or include explicit address in your message.", "send_now": False}
        if not refreshed["body"]:
            return {"handled": True, "reply": f"Draft ready for {refreshed['to']}. What should I say?", "send_now": False}
        return {
            "handled": True,
            "reply": f"Draft ready for {refreshed['to']}. Subject: {refreshed['subject']}. Attachment: {'yes' if refreshed['attachment'] else 'no'}. Say 'send it now' to send.",
            "send_now": False,
        }

        # non-send follow-up: summarize draft state
        _set_email_state(state, chat_id, EmailWorkflowState.DRAFT_READY)
        return {
            "handled": True,
            "reply": f"Draft email ready: to={draft.get('to')} subject={draft.get('subject')} attachment={draft.get('attachment') or 'none'}. Say 'send it now' to send.",
            "send_now": False,
        }

    if not is_email_intent(text) and not followup_send:
        return {"handled": False}

    resolved_to = _resolve_recipient_email(text, state)
    is_send_email_phrase = ("send" in lower) and ("email" in lower or "mail" in lower)
    explicit_target_reference = is_send_email_phrase and (
        (" to " in lower)
        or bool(re.search(r"\b(him|her|them)\b", lower))
    )
    to_addr = resolved_to or ("" if explicit_target_reference else (draft.get("to") or ""))
    if not to_addr:
        return {"handled": True, "reply": f"Please provide recipient email (e.g. {_recipient_hint(state)}).", "send_now": False}
    if _is_placeholder_email(to_addr):
        return {"handled": True, "reply": "Recipient looks like a placeholder address. Please provide a real email.", "send_now": False}
    if not _is_recipient_allowlisted(to_addr, text, state):
        return {"handled": True, "reply": "Recipient is not allowlisted. Set it via /remember email_alias_* or default_recipient_email, or include explicit address in your message.", "send_now": False}

    # inline subject/body extraction (optional)
    inline_subject, inline_body = _extract_subject_body_inline(text)
    subject = inline_subject or draft.get("subject") or "Message from Xavi"

    body = inline_body or extract_email_body(text)
    if not body and draft.get("body") and ("send" in lower):
        body = draft.get("body")

    needs_attachment = wants_attachment(text) or bool(draft.get("attachment"))

    if needs_attachment and chat_id not in attachment_store and not draft.get("attachment"):
        pending = {
            "action": "send_email",
            "to": to_addr,
            "subject": subject,
            "body": body,
        }
        state.pending_action_by_chat[chat_id] = pending
        _persist_workflow_state(state, chat_id, "pending_action", pending)
        _set_email_state(state, chat_id, EmailWorkflowState.AWAITING_ATTACHMENT)
        if hasattr(state, "last_email_payload_by_chat"):
            persisted = {"to": to_addr, "subject": subject, "body": body, "attachment": None}
            stamped = _stamp_draft(persisted)
            state.last_email_payload_by_chat[chat_id] = stamped
            _persist_workflow_state(state, chat_id, "last_email_payload", stamped)
        return {"handled": True, "reply": f"Ready to send to {to_addr}. Please attach the file/photo now.", "send_now": False}

    attachment = attachment_store.get(chat_id) if needs_attachment else draft.get("attachment")
    payload = {"to": to_addr, "subject": subject, "body": body, "attachment": attachment}
    if hasattr(state, "last_email_payload_by_chat"):
        stamped = _stamp_draft(payload)
        state.last_email_payload_by_chat[chat_id] = stamped
        _persist_workflow_state(state, chat_id, "last_email_payload", stamped)

    explicit_send = ("send now" in lower) or ("send email" in lower and " now" in lower)
    if not explicit_send:
        _set_email_state(state, chat_id, EmailWorkflowState.DRAFT_READY)
        return {
            "handled": True,
            "reply": f"Draft ready for {to_addr}. Subject: {subject}. Attachment: {'yes' if attachment else 'no'}. Say 'send it now' to send.",
            "send_now": False,
        }

    _set_email_state(state, chat_id, EmailWorkflowState.CONFIRMED)
    return {
        "handled": True,
        "reply": None,
        "send_now": True,
        "to": to_addr,
        "subject": subject,
        "body": body,
        "attachment": attachment,
    }


def handle_attachment_arrival(chat_id: int, local_path: str, state) -> Optional[dict]:
    """If a pending action can be completed with this attachment, return send request dict."""
    pending = state.pending_action_by_chat.get(chat_id)
    if not pending:
        return None
    if pending.get("action") != "send_email":
        return None

    state.pending_action_by_chat.pop(chat_id, None)
    _persist_workflow_state(state, chat_id, "pending_action", None)
    payload = {
        "to": pending["to"],
        "subject": pending["subject"],
        "body": pending["body"],
        "attachment": local_path,
    }
    if hasattr(state, "last_email_payload_by_chat"):
        stamped = _stamp_draft(payload)
        state.last_email_payload_by_chat[chat_id] = stamped
        _persist_workflow_state(state, chat_id, "last_email_payload", stamped)
    _set_email_state(state, chat_id, EmailWorkflowState.DRAFT_READY)
    return payload
