"""Typed boundary between model providers and the agent runtime."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelCapabilities:
    """Provider-advertised limits and protocol features.

    Unknown limits stay ``None``.  The runtime must not invent a model limit
    from its name; callers may supply authoritative values from provider
    configuration when the compatible API does not expose them.
    """

    context_window: int | None = None
    max_output_tokens: int | None = None
    output_limit_required: bool = False
    supports_reasoning: bool = False
    supports_native_tools: bool = False
    supports_previous_response_id: bool = False
    supports_server_compaction: bool = False


@dataclass(frozen=True)
class ModelToolCall:
    name: str
    args: dict = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class ModelTurn:
    kind: str
    text: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    call_id: str = ""
    tool_calls: tuple[ModelToolCall, ...] = field(default_factory=tuple)
    response_id: str = ""
    output_items: tuple = field(default_factory=tuple)
    response_status: str = ""
    incomplete_reason: str = ""
    protocol_error: str = ""
