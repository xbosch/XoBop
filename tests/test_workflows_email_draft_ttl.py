from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_stale_email_draft_is_expired_before_followup(monkeypatch):
    monkeypatch.setenv("EMAIL_DRAFT_TTL_SECONDS", "1")
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "recipient@example.com", "subject": "s", "body": "b", "attachment": None, "draft_ts": 1}},
        pending_action_by_chat={1: {"action": "send_email", "to": "recipient@example.com"}},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={1: "DRAFT_READY"},
    )

    out = handle_email_text("send it now", 1, state)
    assert out["handled"] is False
    assert 1 not in state.last_email_payload_by_chat
    assert 1 not in state.pending_action_by_chat
