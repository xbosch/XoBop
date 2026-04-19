from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_do_it_does_not_hijack_email_workflow():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "neri1982@gmail.com", "subject": "Hi", "body": "Body", "attachment": None}},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )

    out = handle_email_text("do it", 1, state)
    assert out["handled"] is False


def test_yes_does_not_hijack_email_workflow():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={1: {"to": "neri1982@gmail.com", "subject": "Hi", "body": "Body", "attachment": None}},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )

    out = handle_email_text("yes", 1, state)
    assert out["handled"] is False
