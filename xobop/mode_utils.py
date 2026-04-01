from __future__ import annotations


def temperature_for_mode(mode: str) -> float:
    return 0.05 if mode == "strict" else 0.15


def mode_help_text(current_mode: str) -> str:
    return f"Current mode: *{current_mode}*. Use `/mode strict`, `/mode normal`, or `/mode overseer`."
