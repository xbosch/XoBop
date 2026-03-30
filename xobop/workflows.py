from __future__ import annotations

from typing import Optional

from .intents import (
    extract_email_body,
    extract_recipient_email,
    is_email_intent,
    is_email_status_intent,
    wants_attachment,
)


def handle_email_text(text: str, chat_id: int, state) -> dict:
    """Return workflow decision dict.

    Keys:
      handled: bool
      reply: Optional[str]
      send_now: bool
      to, subject, body, attachment
    """
    if is_email_status_intent(text):
        status = state.last_email_status_by_chat.get(chat_id)
        return {"handled": True, "reply": status or "Not sent yet.", "send_now": False}

    if not is_email_intent(text):
        return {"handled": False}

    to_addr = extract_recipient_email(text)
    if not to_addr:
        return {"handled": True, "reply": "Please provide recipient email (or say wife/neri).", "send_now": False}

    body = extract_email_body(text)
    subject = "Message from Xavi"
    needs_attachment = wants_attachment(text)

    if needs_attachment and chat_id not in state.last_attachment_by_chat:
        state.pending_action_by_chat[chat_id] = {
            "action": "send_email",
            "to": to_addr,
            "subject": subject,
            "body": body,
        }
        return {"handled": True, "reply": f"Ready to send to {to_addr}. Please attach the file/photo now.", "send_now": False}

    attachment = state.last_attachment_by_chat.get(chat_id) if needs_attachment else None
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
    return {
        "to": pending["to"],
        "subject": pending["subject"],
        "body": pending["body"],
        "attachment": local_path,
    }
