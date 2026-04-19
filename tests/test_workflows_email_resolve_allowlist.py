from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_inline_unknown_email_is_rejected_by_allowlist_resolution():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
        fact_getter=lambda _k: "",
        all_facts_getter=lambda: {},
        fact_items_getter=lambda: {},
    )

    out = handle_email_text("send email to random@foo.tld subject: hi body: hello", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    assert "provide recipient" in out["reply"].lower()


def test_inline_alias_email_is_accepted_when_allowlisted():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
        fact_getter=lambda k: "" if k != "default_recipient_email" else "",
        all_facts_getter=lambda: {"email_alias_wife": "neri1982@gmail.com"},
        fact_items_getter=lambda: {"email_alias_wife": "neri1982@gmail.com"},
    )

    out = handle_email_text("send email to neri1982@gmail.com subject: hi body: hello", 1, state)
    assert out["handled"] is True
    assert out["send_now"] is False
    payload = state.last_email_payload_by_chat[1]
    assert payload["to"] == "neri1982@gmail.com"
