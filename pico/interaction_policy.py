"""Structured human-agent interaction policy and workspace preferences."""

import ast
import fnmatch
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from .file_lock import FileLock
from .text_document import TextDecodingError, read_text_document

PACKAGE_LAYOUTS = frozenset({"follow_repository", "layer_first", "feature_first"})
PREFERENCE_SCHEMA_VERSION = 1

_MUTATION = re.compile(
    r"(?i)\b(create|write|modify|change|fix|add|delete|remove|replace|update|patch|finish|"
    r"recover|refactor|implement|rename|run|test|build|commit|apply|migrate)\b|"
    r"创建|写入|修改|更改|修复|新增|删除|替换|更新|补丁|完成|恢复|重构|实现|重命名|"
    r"运行|测试|构建|提交|应用|迁移"
)
_READ_ONLY_CONSTRAINT = re.compile(
    r"(?i)\b(?:do\s+not|don't|without)\s+(?:make\s+\w+\s+)?"
    r"(?:modify|change|write|edit|delete|remove)\b(?:\s+(?:any\s+)?files?)?|"
    r"(?:不要|不得|无需)(?:对[^，。；\n]{0,20})?(?:修改|更改|写入|编辑|删除)(?:任何)?文件"
)
_GLOBAL_READ_ONLY_CONSTRAINT = re.compile(
    r"(?i)\b(?:do\s+not|don't|without)\s+(?:make\s+\w+\s+)?"
    r"(?:modify|change|write|edit|delete|remove)\s+(?:any\s+)?"
    r"(?:files?|code|workspace|repository|repo)\b|"
    r"(?:不要|不得|无需|禁止)(?:修改|更改|写入|编辑|删除)(?:任何)?"
    r"(?:文件|代码|工作区|仓库|项目)"
)
_PLAN = re.compile(r"(?i)\b(plan|design|proposal|approach)\b|方案|设计|规划|怎么做|如何实现")
_REVIEW = re.compile(r"(?i)\b(review|audit|assess|evaluate)\b|评审|审查|检查代码|评估")
_EXPLAIN = re.compile(
    r"(?i)\b(what|which|where|who|why|inspect|explain|analy[sz]e|find|locate|tell me|read)\b|"
    r"是什么|什么是|哪个|哪里|为什么|查看|分析|解释|查找|告诉我|读取"
)
_RELATIVE = re.compile(
    r"(?i)\b(?:a\s+little|slightly|somewhat|smaller|larger|faster|slower|simpler|stricter)\b|"
    r"小一点|大一点|快一点|慢一点|简单一点|严格一点|稍微|略微|适当|一些"
)
_VALIDATION_REQUIRED = re.compile(
    r"(?i)\b(?:run|execute)\s+(?:the\s+)?(?:tests?|build|verification|checks?)\b|"
    r"\bverify\b|(?:运行|执行|跑)(?:[^，。；\n]{0,24})?(?:测试|构建|校验|验证)|"
    r"(?:测试|构建)(?:[^，。；\n]{0,12})?(?:通过|成功)"
)
_TEST_ARTIFACT_REQUIRED = re.compile(
    r"(?i)\b(?:add|create|write|implement|update)\b[^.\n]{0,48}\btests?\b|"
    r"(?:新增|添加|创建|编写|实现|更新)[^，。；\n]{0,24}(?:测试|用例)"
)
_TEMPORARY = re.compile(r"(?i)\b(?:this\s+time|this\s+task|temporarily|for\s+now)\b|本次|这次|本轮|暂时|临时")
_DURABLE = re.compile(r"(?i)\b(?:from\s+now\s+on|always|default|remember|long[- ]term)\b|以后|今后|默认|长期|记住")
_PROTECTED_TARGET = re.compile(
    r"(?i)(?:do\s+not|don't|must\s+not|never)\s+"
    r"(?:modify|change|edit|write|touch|delete)\s+([^.;\n]+)|"
    r"(?:不要|不得|禁止)(?:修改|更改|编辑|写入|触碰|删除)([^，。；\n]+)"
)
_PATH_TOKEN = re.compile(r"[\w./\\*?\[\]-]+\.[A-Za-z0-9*?]+")


class PreferenceError(RuntimeError):
    """Raised when a structured workspace preference cannot be loaded or changed."""


@dataclass(frozen=True)
class InteractionIntent:
    mode: str
    request_profile: str
    mutation_allowed: bool
    relative_adjustment: bool
    override_scope: str

    def to_dict(self):
        return asdict(self)


def classify_interaction(user_message):
    text = str(user_message or "").strip()
    intent_text = _READ_ONLY_CONSTRAINT.sub("", text)
    mutation_allowed = bool(_MUTATION.search(intent_text)) and not bool(
        _GLOBAL_READ_ONLY_CONSTRAINT.search(text)
    )
    if mutation_allowed:
        mode = "implement"
    elif _REVIEW.search(text):
        mode = "review"
    elif _PLAN.search(text):
        mode = "plan"
    elif _EXPLAIN.search(text):
        mode = "explain"
    else:
        mode = "discuss"
    request_profile = (
        "simple_read_only"
        if not mutation_allowed and len(text) <= 280 and mode in {"plan", "review", "explain"}
        else "standard"
    )
    if _DURABLE.search(text):
        override_scope = "durable"
    elif _TEMPORARY.search(text):
        override_scope = "task"
    else:
        override_scope = "unspecified"
    return InteractionIntent(
        mode=mode,
        request_profile=request_profile,
        mutation_allowed=mutation_allowed,
        relative_adjustment=bool(_RELATIVE.search(text)),
        override_scope=override_scope,
    )


def extract_protected_paths(user_message):
    """Extract explicit task-local no-write constraints from the current request."""
    patterns = set()
    for match in _PROTECTED_TARGET.finditer(str(user_message or "")):
        target = str(match.group(1) or match.group(2) or "").strip().casefold()
        if re.search(r"\btests?\b|测试(?:文件|代码|目录)?", target):
            patterns.update({"test*", "test*/**", "tests/**", "**/test*", "**/*test.java", "src/test/**"})
        for token in _PATH_TOKEN.findall(target):
            patterns.add(token.replace("\\", "/").lstrip("./"))
    return sorted(patterns)


def path_matches_patterns(path, patterns):
    normalized = str(path or "").replace("\\", "/").lstrip("./").casefold()
    return any(fnmatch.fnmatchcase(normalized, str(pattern).casefold()) for pattern in patterns or ())


def is_test_artifact_path(path):
    """Return whether a changed path is conventionally an executable test artifact."""
    normalized = str(path or "").replace("\\", "/").strip("/").casefold()
    if not normalized:
        return False
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith(("test/", "tests/", "src/test/"))
        or "/test/" in normalized
        or "/tests/" in normalized
        or name.startswith("test_")
        or name.endswith(("_test.py", "test.java", "tests.java", "spec.js", "spec.ts"))
    )


def is_executable_test_artifact(root, path):
    """Recognize conventional tests or source that contains executable test structure."""
    if is_test_artifact_path(path):
        return True
    candidate = Path(root) / str(path)
    if candidate.suffix.casefold() not in {".py", ".java", ".js", ".jsx", ".ts", ".tsx"}:
        return False
    try:
        text = read_text_document(candidate).text
    except (OSError, TextDecodingError):
        return False
    if candidate.suffix.casefold() == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        return any(
            isinstance(node, ast.Assert)
            or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            for node in ast.walk(tree)
        )
    if candidate.suffix.casefold() == ".java":
        return bool(re.search(r"(?m)^\s*@(?:org\.junit\.)?(?:Test|ParameterizedTest)\b", text))
    return bool(re.search(r"(?m)\b(?:describe|test|it)\s*\(", text))


class WorkspacePreferenceStore:
    DEFAULTS: ClassVar[dict[str, str]] = {"package_layout": "follow_repository"}

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return dict(self.DEFAULTS)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PreferenceError(f"workspace preferences are corrupt: {self.path} ({exc})") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != PREFERENCE_SCHEMA_VERSION:
            raise PreferenceError(f"workspace preferences use an unsupported schema: {self.path}")
        values = dict(self.DEFAULTS)
        stored = payload.get("values", {})
        if not isinstance(stored, dict):
            raise PreferenceError(f"workspace preference values are invalid: {self.path}")
        package_layout = str(stored.get("package_layout", values["package_layout"]))
        if package_layout not in PACKAGE_LAYOUTS:
            raise PreferenceError(f"unsupported package_layout in {self.path}: {package_layout}")
        values["package_layout"] = package_layout
        return values

    def _write(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": PREFERENCE_SCHEMA_VERSION, "values": values}
        temp_path = None
        with FileLock(self.path.with_suffix(self.path.suffix + ".lock")):
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", delete=False, dir=self.path.parent,
                    prefix=self.path.name + ".", suffix=".tmp",
                ) as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    temp_path = Path(handle.name)
                os.replace(temp_path, self.path)
                temp_path = None
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)

    def set_package_layout(self, value):
        value = str(value).strip().lower()
        if value not in PACKAGE_LAYOUTS:
            choices = ", ".join(sorted(PACKAGE_LAYOUTS))
            raise PreferenceError(f"package_layout must be one of: {choices}")
        values = {"package_layout": value}
        self._write(values)
        return values

    def reset_package_layout(self):
        values = dict(self.DEFAULTS)
        self._write(values)
        return values


def build_interaction_contract(user_message, package_layout):
    intent = classify_interaction(user_message)
    return {
        **intent.to_dict(),
        "validation_required": bool(_VALIDATION_REQUIRED.search(str(user_message or ""))),
        "test_artifact_required": bool(
            _TEST_ARTIFACT_REQUIRED.search(str(user_message or ""))
        ),
        "protected_paths": extract_protected_paths(user_message),
        "package_layout": package_layout,
        "instruction_priority": [
            "current_user_request",
            "current_task_constraints",
            "session_context",
            "workspace_preferences",
            "durable_memory",
            "model_inference",
        ],
        "architecture_policy": (
            "Use the selected package layout. When it is follow_repository, inspect analogous existing "
            "features and preserve the repository's dominant structure and dependency direction."
        ),
        "scope_policy": (
            "Change only requested or demonstrably required files. Report required scope expansion; do not "
            "perform opportunistic refactors."
        ),
        "quality_policy": (
            "Preserve low coupling and high cohesion. Extract a named component only for a stable, testable "
            "responsibility; do not create a generic utility dumping ground."
        ),
        "proportionality_policy": (
            "Interpret comparative requests as one reasonable step from the current baseline, while preserving "
            "correctness, security, compatibility, and architecture."
        ),
        "memory_policy": (
            "The current request outranks retrieved memory. Treat conflicts as task-local unless the user "
            "explicitly requests a durable update. If a current instruction conflicts with a durable user "
            "preference and the intended scope is unclear, ask one concise scope question before acting."
        ),
    }
