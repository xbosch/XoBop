from types import SimpleNamespace

from xobop.workflows import handle_attachment_arrival, handle_email_text


def _state():
    return SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
    )


def test_email_draft_attachment_then_send_now_flow():
    state = _state()

    out1 = handle_email_text(
        "send email to neri1982@gmail.com subject: hello body: see attached please attach",
        1,
        state,
    )
    assert out1["handled"] is True
    assert out1["send_now"] is False
    assert "attach" in out1["reply"].lower()

    payload = handle_attachment_arrival(1, "/tmp/file.pdf", state)
    assert payload is not None
    assert payload["attachment"] == "/tmp/file.pdf"

    out2 = handle_email_text("send it now", 1, state)
    assert out2["handled"] is True
    assert out2["send_now"] is True
    assert out2["to"] == "neri1982@gmail.com"
    assert out2["subject"] == "hello"
    assert out2["body"] == "see attached please attach"
    assert out2["attachment"] == "/tmp/file.pdf"
