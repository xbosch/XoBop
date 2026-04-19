from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_send_email_inline_subject_and_body_are_split_correctly():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )
    out = handle_email_text(
        "send email to demo@local.test subject: test body: hello",
        1,
        state,
    )
    assert out["handled"] is True
    assert out["send_now"] is False
    payload = state.last_email_payload_by_chat[1]
    assert payload["subject"] == "test"
    assert payload["body"] == "hello"
