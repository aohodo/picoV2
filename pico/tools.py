"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import difflib
import re
from functools import partial

from .execution import format_shell_result
from .mutation_observation import render_mutation_observation
from .path_support import logical_path, native_path
from .progress import is_repository_read_argv, is_repository_read_command
from .read_observation import DEFAULT_SOURCE_WINDOW_LINES, render_reads
from .text_document import TextDecodingError, read_text_document, write_text_document
from .verification_evidence import VerificationObservation, VerificationProbe
from .workspace import IGNORED_PATH_NAMES

BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {
            "path": "str",
            "start": "int=1",
            "end": f"int={DEFAULT_SOURCE_WINDOW_LINES}",
        },
        "risky": False,
        "description": "Read a text file by line range while detecting its encoding.",
    },
    "read_files": {
        "schema": {"paths": "list[str]"},
        "risky": False,
        "description": "Read several text files in one bounded call.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Search using a case-insensitive Python regular expression. Escape punctuation for literal matching. Returns file:line evidence.",
    },
    "inspect_repository": {
        "schema": {"query": "str", "limit": "int=12"},
        "risky": False,
        "description": (
            "Find relevant Python/Java files using symbols, imports, and reverse dependencies. "
            "Use before broad repository exploration."
        ),
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20"},
        "risky": True,
        "description": (
            "Run a non-inspection command in the transaction workspace using the declared shell profile. "
            "Use typed repository tools for file listing, search, and source reads."
        ),
    },
    "run_verification": {
        "schema": {
            "argv": "list[str]",
            "timeout": "int=120",
            "purpose": "str='acceptance'",
        },
        "risky": True,
        "description": (
            "Run one test, build, lint, or type-check executable directly and record its real exit status. "
            "Use purpose=acceptance for delivery checks and purpose=diagnostic only for exploratory "
            "probes; diagnostic results neither satisfy nor block delivery. Repository inspection "
            "commands are rejected."
        ),
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": (
            "Write a text file and return a bounded diff plus current post-edit source. "
            "Do not reread solely to confirm the edit."
        ),
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": (
            "Replace one exact text block and return a bounded diff plus current post-edit source. "
            "Do not reread solely to confirm the edit."
        ),
    },
}


class PatchMatchError(ValueError):
    """An exact patch miss carrying fresh source evidence for the repair turn."""

    code = "patch_match_failed"

    def __init__(self, message, observation):
        super().__init__(f"{message}\n{observation}")
        self.coverage = observation.coverage

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | {"delegate"}

TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "read_files": '<tool>{"name":"read_files","args":{"paths":["README.md","pyproject.toml"]}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "inspect_repository": '<tool>{"name":"inspect_repository","args":{"query":"UserService create user","limit":12}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"python -m pytest -q","timeout":20}}</tool>',
    "run_verification": (
        '<tool>{"name":"run_verification","args":{"argv":["python","-m","pytest","-q"],'
        '"timeout":120}}</tool>'
    ),
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
}


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, context)}
    return tools


def native_tool_definitions(tools):
    """Convert Pico's allowlisted tools to Responses function definitions."""
    definitions = []
    for name, tool in tools.items():
        properties = {}
        required = []
        for field, type_spec in tool["schema"].items():
            spec = str(type_spec)
            base = spec.split("=", 1)[0]
            if base == "int":
                schema = {"type": "integer"}
            elif base == "list[str]":
                schema = {"type": "array", "items": {"type": "string"}, "minItems": 1}
            else:
                schema = {"type": "string"}
            if name in {"run_shell", "run_verification"} and field == "timeout":
                schema.update({"minimum": 1, "maximum": 120})
            elif name == "delegate" and field == "max_steps":
                schema.update({"minimum": 1, "maximum": 12})
            elif name == "inspect_repository" and field == "limit":
                schema.update({"minimum": 1, "maximum": 30})
            properties[field] = schema
            if "=" not in spec:
                required.append(field)
        definitions.append(
            {
                "type": "function",
                "name": name,
                "description": tool["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    return definitions


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not native_path(path).is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not native_path(path).is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", context.source_window_lines))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "read_files":
        paths = args.get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 12:
            raise ValueError("paths must be a non-empty list with at most 12 items")
        for raw_path in paths:
            path = context.path(raw_path)
            if not native_path(path).is_file():
                raise ValueError(f"path is not a file: {raw_path}")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        context.path(args.get("path", "."))
        return

    if name == "inspect_repository":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = int(args.get("limit", 12))
        if limit < 1 or limit > 30:
            raise ValueError("limit must be in [1, 30]")
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        if is_repository_read_command(command):
            raise ValueError(
                "repository inspection command; use list_files, read_file, read_files, "
                "search, or inspect_repository"
            )
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "run_verification":
        argv = args.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 64
            or any(not isinstance(item, str) or not item.strip() for item in argv)
        ):
            raise ValueError("argv must be a non-empty list of at most 64 non-empty strings")
        if is_repository_read_argv(argv):
            raise ValueError(
                "argv is a repository inspection command, not verification; use list_files, "
                "read_file, read_files, search, or inspect_repository"
            )
        timeout = int(args.get("timeout", 120))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        purpose = str(args.get("purpose", "acceptance")).strip().lower()
        if purpose not in {"acceptance", "diagnostic"}:
            raise ValueError("purpose must be 'acceptance' or 'diagnostic'")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if native_path(path).exists() and native_path(path).is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not native_path(path).is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    io_path = native_path(path)
    if not io_path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        logical_path(item) for item in sorted(io_path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not native_path(path).is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", context.source_window_lines))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    document = read_text_document(path)
    lines = document.text.splitlines()
    return render_reads([(path.relative_to(context.root).as_posix(), document.encoding,
                          lines, start, end)], context.observation_char_budget)


def tool_read_files(context, args):
    paths = args.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 12:
        raise ValueError("paths must be a non-empty list with at most 12 items")
    documents = []
    for raw_path in paths:
        path = context.path(raw_path)
        document = read_text_document(path)
        documents.append((path.relative_to(context.root).as_posix(), document.encoding,
                          document.text.splitlines(), 1, context.source_window_lines))
    return render_reads(documents, context.observation_char_budget)


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    expression = re.compile(pattern, re.IGNORECASE)
    path = context.path(args.get("path", "."))

    matches = []
    search_root = native_path(path)
    files = [path] if search_root.is_file() else [
        logical_path(item) for item in search_root.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in logical_path(item).relative_to(context.root).parts)
    ]
    for file_path in files:
        try:
            document = read_text_document(file_path)
        except (OSError, TextDecodingError):
            continue
        for number, line in enumerate(document.text.splitlines(), start=1):
            if expression.search(line):
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_inspect_repository(context, args):
    query = str(args.get("query", ""))
    limit = int(args.get("limit", 12))
    if context.repository_inspector is not None:
        return context.repository_inspector(query, limit).render()
    from .repository_graph import RepositoryGraph

    return RepositoryGraph(context.root).query(query, limit=limit)


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    if context.command_runner is None:
        raise RuntimeError("shell_runtime_unavailable: no execution runtime is attached to this transaction")
    return format_shell_result(
        context.command_runner.run(command, timeout=timeout),
        char_budget=context.observation_char_budget,
    )


def tool_run_verification(context, args):
    argv = args.get("argv")
    timeout = int(args.get("timeout", 120))
    if context.command_runner is None:
        raise RuntimeError("shell_runtime_unavailable: no execution runtime is attached to this transaction")
    probe = VerificationProbe(context.root, argv, context.pending_test_paths())
    result = context.command_runner.run_argv(probe.process_argv(), timeout=timeout)
    evidence = probe.finish()
    output = format_shell_result(result, char_budget=context.observation_char_budget)
    if result["exit_code"] == 0 and evidence["missing_test_paths"]:
        output += (
            "\nverification_incomplete: the process succeeded, but no execution evidence "
            "covered these changed test artifacts: "
            + ", ".join(evidence["missing_test_paths"])
            + ". Run those tests explicitly or use a runner that emits test reports."
        )
    return VerificationObservation(output, evidence)


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    existing = read_text_document(path) if native_path(path).is_file() else None
    write_text_document(path, content, existing)
    relative = path.relative_to(context.root).as_posix()
    return render_mutation_observation(
        relative,
        existing.text if existing is not None else "",
        read_text_document(path),
        "write_file",
        context.observation_char_budget,
        context.source_window_lines,
    )


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not native_path(path).is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    document = read_text_document(path)
    text = document.text
    count = text.count(old_text)
    if count != 1:
        lines = text.splitlines()
        relative = path.relative_to(context.root).as_posix()
        if count:
            occurrences = []
            offset = 0
            while True:
                offset = text.find(old_text, offset)
                if offset < 0:
                    break
                occurrences.append(text.count("\n", 0, offset) + 1)
                offset += max(1, len(old_text))
            center = occurrences[0]
            detail = f"old_text matched {count} times at lines {occurrences[:8]}"
        else:
            anchors = [line.strip() for line in old_text.splitlines() if line.strip()]
            anchor = max(anchors, key=len, default="")
            center = 1
            if anchor and lines:
                center = max(
                    range(1, len(lines) + 1),
                    key=lambda number: difflib.SequenceMatcher(
                        None, anchor, lines[number - 1].strip()
                    ).ratio(),
                )
            detail = "old_text matched 0 times"
        radius = max(1, context.source_window_lines // 2)
        start = max(1, center - radius)
        end = min(len(lines), start + context.source_window_lines - 1)
        observation = render_reads(
            [(relative, document.encoding, lines, start, end)],
            context.observation_char_budget,
        )
        raise PatchMatchError(
            f"{detail}. Construct one new exact old_text from the current source below; "
            "do not reread solely to recover this patch.",
            observation,
        )
    write_text_document(path, text.replace(old_text, str(args["new_text"]), 1), document)
    relative = path.relative_to(context.root).as_posix()
    return render_mutation_observation(
        relative,
        document.text,
        read_text_document(path),
        "patch_file",
        context.observation_char_budget,
        context.source_window_lines,
    )


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_files": tool_read_files,
    "search": tool_search,
    "inspect_repository": tool_inspect_repository,
    "run_shell": tool_run_shell,
    "run_verification": tool_run_verification,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
}
