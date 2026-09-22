"""Admission control for model-proposed terminal answers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CompletionDecision:
    accepted: bool
    text: str = ""
    reason: str = ""


class CompletionAdmission:
    @staticmethod
    def evaluate(turn):
        if turn.response_status != "completed":
            return CompletionDecision(False, reason=f"response_status={turn.response_status or 'missing'}")
        if turn.kind != "final":
            return CompletionDecision(False, reason=f"turn_kind={turn.kind}")
        text = str(turn.text or "").strip()
        if not text:
            return CompletionDecision(False, reason="empty_final")
        if text in {"<", ">", "</", "<tool", "<final"}:
            return CompletionDecision(False, reason="protocol_fragment")
        if "<tool" in text:
            return CompletionDecision(False, reason="tool_protocol_in_final_message")
        if "<final>" in text or "</final>" in text:
            if not (text.startswith("<final>") and text.endswith("</final>")):
                return CompletionDecision(False, reason="truncated_final_protocol")
            text = text[len("<final>") : -len("</final>")].strip()
            if not text:
                return CompletionDecision(False, reason="empty_final")
        if text.startswith("<") and ">" not in text:
            return CompletionDecision(False, reason="protocol_fragment")
        return CompletionDecision(True, text=text)
