"""Opt-in read-only Qwen acceptance runs against user-provided repositories."""

import sys
from pathlib import Path

from pico.cli import _build_model_client, build_arg_parser
from pico.config import load_project_env
from pico.progress_output import ConsoleProgressRenderer
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext

CASES = {
    "python": (
        "bid-info-push_agent_only/agent",
        (
            "Analyze how JsonSiteConfigRepository.load and save connect to SiteConfig serialization. "
            "Explain missing-file behavior, key validation, and how saving reaches the final JSON file. "
            "Read the related implementations and cite actual file/method/line evidence. Do not modify files."
        ),
    ),
    "java": (
        "carepulse-v6-java21/carepulse-v6",
        (
            "Trace findDTOByUsername from its controller caller through service, mapper and SystemUserDTO. "
            "Explain filtering and missing-user behavior, distinguishing inherited framework behavior "
            "from code visible in this repository. Cite actual file/method/line evidence. Do not modify files."
        ),
    ),
}


def main():
    case = sys.argv[1]
    repository = Path(__file__).resolve().parents[1]
    relative, prompt = CASES[case]
    root = repository / "test" / "real_workspace_test" / relative
    state = repository.parent / "pico-navigation-acceptance" / case
    load_project_env(repository)
    args = build_arg_parser().parse_args(
        ["--provider", "openai", "--openai-timeout", "90"]
    )
    agent = Pico(
        model_client=_build_model_client(args),
        workspace=WorkspaceContext.build(root, repo_root_override=root),
        session_store=SessionStore(state / "sessions"),
        state_root=state,
        read_only=True,
        approval_policy="never",
        max_steps=12,
        hard_discovery_limit=10,
        semantic_index="off",
        progress_sink=ConsoleProgressRenderer(max_steps=12),
    )
    try:
        print(agent.ask(prompt))
        print(f"SESSION: {agent.session_path}")
    finally:
        agent.close_repository_intelligence()


if __name__ == "__main__":
    main()
