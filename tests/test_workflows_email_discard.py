from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_discard_email_clears_payload_and_pending_action():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "recipient@example.com", "subject": "s", "body": "b", "attachment": None}},
        pending_action_by_chat={1: {"action": "send_email", "to": "recipient@example.com"}},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={1: "DRAFT_READY"},
    )

    out = handle_email_text("Discard email", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    assert "discarded" in out["reply"].lower()
    assert 1 not in state.last_email_payload_by_chat
    assert 1 not in state.pending_action_by_chat
