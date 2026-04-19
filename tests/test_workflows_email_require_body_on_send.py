from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_send_now_requires_nonempty_body():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "neri1982@gmail.com", "subject": "Message from Xavi", "body": "", "attachment": None}},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )

    out = handle_email_text("send it now", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    assert "What should I say" in out["reply"] or "body" in out["reply"].lower()
