"""Explicit per-turn model execution policy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnExecutionPolicy:
    reasoning_effort: str
    max_output_tokens: int
    max_contract_retries: int = 1


class ModelExecutionPolicy:
    MODES = frozenset({"adaptive", "fast", "deep"})

    def __init__(self, mode="adaptive"):
        self.mode = str(mode or "adaptive").strip().lower()
        if self.mode not in self.MODES:
            raise ValueError(f"unsupported model execution policy: {self.mode}")

    def for_turn(self, purpose, max_output_tokens, read_only=False, recovering=False):
        maximum = max(16, int(max_output_tokens))
        if self.mode == "fast":
            # Fast is an explicit latency-first choice. Qwen 3.8 defaults to
            # xhigh, so "low" still pays for a reasoning phase; "none" is the
            # provider contract for direct tool routing and short answers.
            effort = "none"
        elif self.mode == "deep":
            effort = "high"
        elif purpose == "finalization" or read_only or recovering:
            effort = "low"
        else:
            effort = "medium"
        return TurnExecutionPolicy(
            reasoning_effort=effort,
            max_output_tokens=maximum,
            max_contract_retries=1,
        )
