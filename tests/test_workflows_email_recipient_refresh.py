from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_explicit_send_to_phrase_uses_resolved_contact_not_stale_draft():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "recipient@example.com", "subject": "old", "body": "old", "attachment": None}},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
        email_contact_resolver=lambda text: "neri1982@gmail.com" if "wife" in text.lower() else None,
    )

    out = handle_email_text("Send email to my wife", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    payload = state.last_email_payload_by_chat[1]
    assert payload["to"] == "neri1982@gmail.com"
