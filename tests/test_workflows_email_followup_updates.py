from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_followup_subject_and_body_updates_draft_before_send():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )

    out1 = handle_email_text("send email to neri1982@gmail.com", 1, state)
    assert out1["handled"] is True and out1["send_now"] is False

    out2 = handle_email_text("subject: School update", 1, state)
    assert out2["handled"] is True and out2["send_now"] is False

    out3 = handle_email_text("body: Marti will arrive 10 minutes late", 1, state)
    assert out3["handled"] is True and out3["send_now"] is False

    out4 = handle_email_text("send now", 1, state)
    assert out4["handled"] is True and out4["send_now"] is True
    assert out4["subject"] == "School update"
    assert out4["body"] == "Marti will arrive 10 minutes late"
