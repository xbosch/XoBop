from types import SimpleNamespace

from xobop.workflows import handle_email_text


def test_multiline_general_prompt_does_not_trigger_email_send():
    state = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
    )
    text = "Hi\nSummarize in one sentence what this bot can do\nNow summarize in 12 words"
    out = handle_email_text(text, 1, state)
    assert out.get("handled") is False
