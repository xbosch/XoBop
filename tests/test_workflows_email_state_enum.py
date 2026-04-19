from types import SimpleNamespace

from xobop.workflows import EmailWorkflowState, handle_email_text


def _state():
    saved = []

    def saver(chat_id, key, value):
        saved.append((chat_id, key, value))

    st = SimpleNamespace(
        last_email_status_by_chat={},
        last_email_payload_by_chat={},
        pending_action_by_chat={},
        last_attachment_by_chat={},
        email_workflow_state_by_chat={},
        workflow_state_saver=saver,
        email_contact_resolver=lambda text: "neri1982@gmail.com" if "wife" in text.lower() else None,
    )
    return st, saved


def test_email_state_transitions_draft_to_confirmed():
    state, _saved = _state()
    out = handle_email_text("send email to my wife", 1, state)
    assert out["handled"] is True and out["send_now"] is False
    assert state.email_workflow_state_by_chat[1] == EmailWorkflowState.DRAFT_READY.value

    out2 = handle_email_text("send it now", 1, state)
    assert out2["handled"] is True and out2["send_now"] is False
    assert "what should i say" in out2["reply"].lower() or "body" in out2["reply"].lower()
    assert state.email_workflow_state_by_chat[1] == EmailWorkflowState.DRAFT_READY.value


def test_email_state_awaiting_attachment_when_needed():
    state, _saved = _state()
    out = handle_email_text("send email to my wife and attach photo", 1, state)
    assert out["handled"] is True and out["send_now"] is False
    assert state.email_workflow_state_by_chat[1] == EmailWorkflowState.AWAITING_ATTACHMENT.value
