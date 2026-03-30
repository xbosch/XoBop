from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class RuntimeState:
    """In-memory runtime state (per chat) for pending workflows and last outcomes."""

    last_exec_by_chat: Dict[int, str] = field(default_factory=dict)
    last_attachment_by_chat: Dict[int, str] = field(default_factory=dict)
    pending_action_by_chat: Dict[int, dict] = field(default_factory=dict)
    last_email_status_by_chat: Dict[int, str] = field(default_factory=dict)


STATE = RuntimeState()
