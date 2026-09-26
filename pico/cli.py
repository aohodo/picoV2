"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

from .config import (
    PicoConfigError,
    load_runtime_env,
    provider_env,
    require_provider_value,
)
from .interaction_policy import PACKAGE_LAYOUTS, PreferenceError
from .progress_output import ConsoleProgressRenderer
from .providers.clients import (
    AnthropicCompatibleModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from .runtime import Pico, SessionStore
from .security import SecretBoundary
from .session_store import SessionError
from .state_root import WorkspaceState
from .workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "pico"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session file.
    /reset   Clear the current session history and memory.
    /preferences                         Show workspace coding preferences.
    /set package-layout <layout>         Set follow_repository, layer_first, or feature_first.
    /unset package-layout                Restore follow_repository.
    /exit    Exit the agent.

    Press Ctrl+C during a run to interrupt it and return to this prompt.
    """
).strip()


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")
SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _effective_provider(args):
    # Provider 选择优先级：
    # 1. 用户显式传入 --provider
    # 2. 项目 .env / shell 里的 PICO_PROVIDER
    provider = getattr(args, "provider", None) or provider_env("PICO_PROVIDER")
    if not provider:
        raise PicoConfigError(
            "PICO_PROVIDER is not configured. Define it in .env or pass --provider; "
            "Pico will not silently select a cloud provider."
        )
    if provider not in PROVIDER_CHOICES:
        choices = ", ".join(PROVIDER_CHOICES)
        raise PicoConfigError(
            f"unknown provider: {provider}. expected one of: {choices}"
        )
    return provider


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("PICO_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        raise PicoConfigError(
            "openai provider requires PICO_OPENAI_MODEL in .env or --model"
        )
    if provider == "anthropic":
        model = provider_env("PICO_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        raise PicoConfigError(
            "anthropic provider requires PICO_ANTHROPIC_MODEL in .env or --model"
        )
    if provider == "deepseek":
        model = provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        raise PicoConfigError(
            "deepseek provider requires PICO_DEEPSEEK_MODEL in .env or --model"
        )
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _optional_positive_int(value, name):
    if value in (None, ""):
        return None
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _build_model_client(args):
    provider = _effective_provider(args)
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = require_provider_value(
            getattr(args, "base_url", None)
            or provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",)),
            "PICO_OPENAI_API_BASE",
            provider,
        )
        api_key = require_provider_value(
            provider_env(
                "PICO_OPENAI_API_KEY",
                ("OPENAI_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY"),
            ),
            "PICO_OPENAI_API_KEY",
            provider,
        )
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            reasoning_effort=(
                getattr(args, "openai_reasoning_effort", None)
                or provider_env("PICO_OPENAI_REASONING_EFFORT")
                or None
            ),
            context_window=_optional_positive_int(
                getattr(args, "model_context_window", None)
                or provider_env("PICO_MODEL_CONTEXT_WINDOW"),
                "model context window",
            ),
            max_output_tokens=_optional_positive_int(
                getattr(args, "model_max_output_tokens", None)
                or provider_env("PICO_MODEL_MAX_OUTPUT_TOKENS"),
                "model maximum output tokens",
            ),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = require_provider_value(
            getattr(args, "base_url", None)
            or provider_env("PICO_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",)),
            "PICO_ANTHROPIC_API_BASE",
            provider,
        )
        api_key = require_provider_value(
            provider_env(
                "PICO_ANTHROPIC_API_KEY",
                ("ANTHROPIC_API_KEY", "PICO_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY"),
            ),
            "PICO_ANTHROPIC_API_KEY",
            provider,
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = require_provider_value(
            getattr(args, "base_url", None)
            or provider_env("PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",)),
            "PICO_DEEPSEEK_API_BASE",
            provider,
        )
        api_key = require_provider_value(
            provider_env("PICO_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",)),
            "PICO_DEEPSEEK_API_KEY",
            provider,
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
            # Pico currently uses text-encoded tool calls and does not replay
            # provider-native thinking blocks. Keep the default transport mode
            # deterministic until that protocol is implemented end to end.
            thinking={"type": "disabled"},
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Pico 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_runtime_env(Path.cwd(), workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    workspace_state = WorkspaceState(workspace.repo_root).ensure()
    boundary = SecretBoundary(secret_env_names=configured_secret_names)
    store = SessionStore(workspace_state.sessions, secret_boundary=boundary)
    legacy_sessions = Path(workspace.repo_root) / ".pico" / "sessions"
    if legacy_sessions.exists():
        for legacy_path in legacy_sessions.glob("*.json"):
            target = store.path(legacy_path.stem)
            if target.exists():
                continue
            try:
                store.save(json.loads(legacy_path.read_text(encoding="utf-8")))
            except (OSError, KeyError, json.JSONDecodeError):
                continue
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            commit_policy=getattr(args, "commit_policy", "review"),
            state_root=workspace_state.global_root,
            soft_discovery_limit=getattr(args, "soft_discovery_limit", None),
            hard_discovery_limit=getattr(args, "hard_discovery_limit", None),
            model_execution_policy=getattr(args, "model_execution_policy", "adaptive"),
            package_layout=getattr(args, "package_layout", None),
            semantic_index=getattr(args, "semantic_index", "auto"),
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        commit_policy=getattr(args, "commit_policy", "review"),
        state_root=workspace_state.global_root,
        soft_discovery_limit=getattr(args, "soft_discovery_limit", None),
        hard_discovery_limit=getattr(args, "hard_discovery_limit", None),
        model_execution_policy=getattr(args, "model_execution_policy", "adaptive"),
        package_layout=getattr(args, "package_layout", None),
        semantic_index=getattr(args, "semantic_index", "auto"),
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for DeepSeek, OpenAI-compatible, Anthropic-compatible, or Ollama models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="Model backend to use. Defaults to PICO_PROVIDER from .env; no implicit cloud provider is selected.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, PICO_OPENAI_MODEL for openai, PICO_ANTHROPIC_MODEL for anthropic, and PICO_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument(
        "--openai-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Responses API reasoning effort; qwen3.8 models default to medium.",
    )
    parser.add_argument(
        "--model-execution-policy",
        choices=("adaptive", "fast", "deep"),
        default="adaptive",
        help="Per-turn thinking policy. An explicit --openai-reasoning-effort still takes precedence.",
    )
    parser.add_argument(
        "--model-context-window",
        type=int,
        default=None,
        help="Authoritative model context-window capability when the compatible API does not publish it.",
    )
    parser.add_argument(
        "--model-max-output-tokens",
        type=int,
        default=None,
        help="Authoritative model output capability; this is not a per-turn target.",
    )
    parser.add_argument(
        "--semantic-index",
        choices=("auto", "off"),
        default="auto",
        help="Use optional Python/Java language-server evidence when pico[lsp] is installed.",
    )
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument(
        "--package-layout",
        choices=tuple(sorted(PACKAGE_LAYOUTS)),
        default=None,
        help="Per-process package layout override; workspace preference is used when omitted.",
    )
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--commit-policy",
        choices=("review", "auto"),
        default="review",
        help="Apply validated staged changes after review, or automatically for CI/benchmarks.",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help=(
            "Maximum tool executions per request; bounded protocol recovery "
            "and finalization turns are accounted for separately."
        ),
    )
    parser.add_argument(
        "--soft-discovery-limit",
        type=int,
        default=None,
        help="Deprecated compatibility option; progress advice no longer depends on a discovery quota.",
    )
    parser.add_argument(
        "--hard-discovery-limit",
        type=int,
        default=None,
        help="Deprecated compatibility option; exploration no longer restricts tools or forces a phase transition.",
    )
    parser.add_argument(
        "--max-output-cap",
        "--max-new-tokens",
        dest="max_new_tokens",
        type=int,
        default=None,
        help=(
            "Optional hard ceiling for a model turn. By default Pico lets the "
            "adaptive policy and provider capabilities choose; --max-new-tokens "
            "is retained as a deprecated alias."
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable live runtime progress events on stderr.",
    )
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def print_safe(value="", file=None):
    stream = file or sys.stdout
    text = str(value)
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        printable = text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")
        print(printable, file=stream)


def review_transaction(agent):
    context = agent.transaction_context
    if context is None or context.workspace.state != "READY_FOR_REVIEW":
        return
    print("\nTransaction READY_FOR_REVIEW")
    for change in context.workspace.diff():
        print(f"- {change['operation']}: {change['path']}")
    try:
        decision = input("Apply staged changes? [y/N/d=discard] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nStaged transaction retained for resume.")
        return
    if decision in {"y", "yes", "apply"}:
        result = agent.apply_transaction()
        print(f"transaction {result['state'].lower()}")
    elif decision in {"d", "discard"}:
        result = agent.discard_transaction()
        print(f"transaction {result['state'].lower()}")
    else:
        print("staged transaction retained for resume")


def run_exit_code(agent):
    outcome = getattr(agent, "last_run_outcome", None)
    if outcome is not None:
        return int(outcome.exit_code)
    task_state = getattr(agent, "current_task_state", None)
    if task_state is None or task_state.exit_code is None:
        return 1
    return int(task_state.exit_code)


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        agent = build_agent(args)
    except (PicoConfigError, SessionError, PreferenceError) as exc:
        print_safe(str(exc), file=sys.stderr)
        return 2

    if not args.no_progress:
        agent.progress_sink = ConsoleProgressRenderer(max_steps=getattr(agent, "max_steps", None))

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))
    # A resumed task may already be waiting for the user's commit decision.
    # Surface that decision before accepting another prompt so the staged work
    # cannot become unreachable behind an active READY_FOR_REVIEW transaction.
    review_transaction(agent)

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print_safe(agent.ask(prompt))
                review_transaction(agent)
            except KeyboardInterrupt:
                print_safe("\nRun interrupted; staged workspace was preserved for the next request.", file=sys.stderr)
                return 130
            except RuntimeError as exc:
                print_safe(str(exc), file=sys.stderr)
                return 1
        return run_exit_code(agent)

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue
        if user_input == "/preferences":
            try:
                print_safe(json.dumps(agent.preferences_view(), ensure_ascii=False, indent=2))
            except PreferenceError as exc:
                print_safe(str(exc), file=sys.stderr)
            continue
        if user_input.startswith("/set package-layout "):
            value = user_input.removeprefix("/set package-layout ").strip()
            try:
                agent.set_workspace_package_layout(value)
                print_safe(f"package-layout set to {value}")
            except PreferenceError as exc:
                print_safe(str(exc), file=sys.stderr)
            continue
        if user_input == "/unset package-layout":
            agent.reset_workspace_package_layout()
            print_safe("package-layout reset to follow_repository")
            continue

        print()
        try:
            print_safe(agent.ask(user_input))
            review_transaction(agent)
        except KeyboardInterrupt:
            print_safe("\nRun interrupted; staged workspace was preserved. Enter the next request.", file=sys.stderr)
        except RuntimeError as exc:
            print_safe(str(exc), file=sys.stderr)
