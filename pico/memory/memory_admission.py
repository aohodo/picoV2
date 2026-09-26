"""Admission control for user-authorized durable memory writes."""

import re
from dataclasses import dataclass, field

_ENGLISH_INTENT = re.compile(
    r"(?is)(?:^|[\n;])\s*(?:please\s+)?(?:remember|save|store|persist|note)"
    r"(?:\s+(?:that|this|these durable facts|the following|as a long[- ]term (?:rule|memory)))?\s*[:：,-]?\s*(.+)"
)
_CHINESE_INTENT = re.compile(
    r"(?s)(?:^|[\n；])\s*(?:请)?(?:长期)?(?:记住|保存|记录|沉淀)"
    r"(?:这个|以下|下来)?(?:长期)?(?:约定|偏好|决策|依赖|事实)?\s*[:：]?\s*(.+)"
)


@dataclass(frozen=True)
class MemoryAdmission:
    action: str
    candidates: tuple = field(default_factory=tuple)
    rejections: tuple = field(default_factory=tuple)


def _statements(text):
    """Return explicit facts without treating surrounding prose as memory."""

    values = []
    for value in re.split(r"[\n。！？]", str(text)):
        value = value.strip(" \t,，;；")
        if value:
            values.append(value)
    return values


def _topic(text):
    lowered = text.lower()
    if re.search(r"(?i)\b(prefer|preference)\b", lowered) or re.search(r"(偏好|希望|习惯)", text):
        return "user-preferences"
    if re.search(r"(?i)\b(depend|dependency|requires?|version|runtime|sdk|jdk)\b", lowered) or re.search(r"(依赖|版本|环境要求|运行时)", text):
        return "dependency-facts"
    if re.search(r"(?i)\b(decide|decision|chosen?)\b", lowered) or re.search(r"(决定|决策|选定)", text):
        return "key-decisions"
    return "project-conventions"


def _subject(text):
    patterns = (
        r"^(.+?)\s+(?:is|are|uses?|should|must|prefers?)\s+.+$",
        r"^(.+?)(?:是|使用|采用|应该|必须|固定为|改用).+$",
    )
    for pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return " ".join(match.group(1).lower().split())[:120]
    tokens = re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]{1,8}", text.lower())
    return " ".join(tokens[:8])[:120]


def extract_explicit_memory(user_message):
    text = str(user_message or "")
    match = _CHINESE_INTENT.search(text) or _ENGLISH_INTENT.search(text)
    if not match:
        return MemoryAdmission("non-write")
    facts = _statements(match.group(1))
    if not facts:
        return MemoryAdmission("non-write", rejections=("empty",))
    labels = (
        ("project-conventions", r"(?i)^project convention\s*[:：]\s*"),
        ("key-decisions", r"(?i)^decision\s*[:：]\s*"),
        ("dependency-facts", r"(?i)^dependency\s*[:：]\s*"),
        ("user-preferences", r"(?i)^preference\s*[:：]\s*"),
        ("project-conventions", r"^项目约定\s*[:：]\s*"),
        ("key-decisions", r"^决策\s*[:：]\s*"),
        ("dependency-facts", r"^依赖\s*[:：]\s*"),
        ("user-preferences", r"^偏好\s*[:：]\s*"),
    )
    candidates = []
    rejections = []
    for fact in facts:
        if re.search(
            r"(?i)^(?:this|these|the\s+(?:stable|updated|following)\s+(?:fact|facts|dependency|decision))\b|"
            r"(?:fact|facts)\s+(?:you|we)\s+(?:already\s+)?(?:found|discovered)\b|^(?:这个|这些|上述|以下)(?:事实|约定|依赖|决策)?(?:保存|记录|记住|到)",
            fact,
        ):
            rejections.append("unresolved_reference")
            continue
        explicit_topic = None
        for topic, pattern in labels:
            stripped, count = re.subn(pattern, "", fact, count=1)
            if count:
                explicit_topic = topic
                fact = stripped.strip()
                break
        if fact:
            candidates.append(
                {
                    "topic": explicit_topic or _topic(fact),
                    "subject": _subject(fact),
                    "text": fact,
                    "source": "user_explicit",
                }
            )
    action = "write" if candidates else "non-write"
    return MemoryAdmission(action, candidates=tuple(candidates), rejections=tuple(rejections))
