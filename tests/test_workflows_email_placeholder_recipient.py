from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_placeholder_recipient_is_rejected():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )

    out = handle_email_text("send email to recipient@example.com subject: hi body: test", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    assert "placeholder" in out["reply"].lower()
