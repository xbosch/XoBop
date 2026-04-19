from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_explicit_non_allowlisted_email_is_blocked():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
        fact_getter=lambda _k: "",
        fact_items_getter=lambda: {},
    )

    out = handle_email_text("send email to random@foo.tld subject: s body: b", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    assert "provide recipient" in out["reply"].lower() or "allowlisted" in out["reply"].lower()
