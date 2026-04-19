from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_nonemail_send_them_phrase_does_not_trigger_email_draft_flow():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "recipient@example.com", "subject": "test", "body": "hello", "attachment": None}},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )

    out = handle_email_text("You send them to me", 1, state)
    assert out["handled"] is False
