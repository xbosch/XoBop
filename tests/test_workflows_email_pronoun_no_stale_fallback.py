from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_send_her_email_does_not_fallback_to_stale_draft_recipient():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "recipient@example.com", "subject": "old", "body": "old", "attachment": None}},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
        email_contact_resolver=lambda text: None,
    )

    out = handle_email_text("Send her an email", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    assert "Please provide recipient email" in out["reply"]
