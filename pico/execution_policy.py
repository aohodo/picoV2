"""Explicit per-turn model execution policy."""

import re
from dataclasses import dataclass

_MUTATION_INTENT = re.compile(
    r"(?i)\b(create|write|modify|change|fix|add|delete|remove|refactor|implement|rename|"
    r"run|test|build|commit)\b|创建|写入|修改|更改|修复|新增|删除|重构|实现|重命名|运行|测试|构建|提交"
)
_READ_INTENT = re.compile(
    r"(?i)\b(what|which|where|who|inspect|explain|analy[sz]e|find|locate|tell me|read)\b|"
    r"是什么|什么是|哪个|哪里|查看|分析|解释|查找|告诉我|读取"
)
_READ_ONLY_CONSTRAINT = re.compile(
    r"(?i)\b(?:do\s+not|don't|without)\s+(?:make\s+\w+\s+)?(?:modify|change|write|edit|delete|remove)\b"
    r"(?:\s+(?:any\s+)?files?)?|(?:不要|不得|无需)(?:对[^，。；\n]{0,20})?(?:修改|更改|写入|编辑|删除)(?:任何)?文件"
)


def classify_request(user_message):
    text = str(user_message or "").strip()
    intent_text = _READ_ONLY_CONSTRAINT.sub("", text)
    if text and len(text) <= 280 and _READ_INTENT.search(text) and not _MUTATION_INTENT.search(intent_text):
        return "simple_read_only"
    return "standard"


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

    def for_turn(self, purpose, max_output_tokens, read_only=False, recovering=False, request_profile="standard"):
        maximum = max(16, int(max_output_tokens))
        if self.mode == "fast":
            # Fast is an explicit latency-first choice. Qwen 3.8 defaults to
            # xhigh, so "low" still pays for a reasoning phase; "none" is the
            # provider contract for direct tool routing and short answers.
            effort = "none"
        elif self.mode == "deep":
            effort = "high"
        elif purpose == "finalization" or read_only or recovering or request_profile == "simple_read_only":
            effort = "low"
        else:
            effort = "medium"
        return TurnExecutionPolicy(
            reasoning_effort=effort,
            max_output_tokens=maximum,
            max_contract_retries=1,
        )
