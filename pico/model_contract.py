"""Typed boundary between model providers and the agent runtime."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelTurn:
    kind: str
    text: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    call_id: str = ""
    response_id: str = ""
    output_items: tuple = field(default_factory=tuple)
    response_status: str = ""
    incomplete_reason: str = ""
    protocol_error: str = ""
