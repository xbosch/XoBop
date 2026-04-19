from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_email_status_without_history_is_actionable():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
    )
    out = handle_email_text("what did you send?", 1, state)
    assert out["handled"] is True
    assert "Start a draft" in out["reply"]
