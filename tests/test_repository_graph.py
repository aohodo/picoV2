import asyncio
import time

import pytest

from pico.providers.clients import FakeModelClient
from pico.repository_graph import RepositoryGraph
from pico.repository_intelligence import (
    MultilspySemanticBackend,
    RepositoryIntelligence,
    SemanticEvidence,
    SemanticLocation,
)
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


def test_python_graph_reports_symbols_and_reverse_dependencies(tmp_path):
    (tmp_path / "service.py").write_text(
        "def calculate_total(values):\n    return sum(values)\n",
        encoding="utf-8",
    )
    (tmp_path / "controller.py").write_text(
        "from service import calculate_total\n\ndef handle(values):\n    return calculate_total(values)\n",
        encoding="utf-8",
    )

    result = RepositoryGraph(tmp_path).query("calculate_total")

    assert "service.py" in result
    assert "function:calculate_total@1" in result
    assert "referenced_by: controller.py" in result


def test_java_graph_reports_controller_service_mapper_chain(tmp_path):
    java_root = tmp_path / "src" / "main" / "java" / "example"
    java_root.mkdir(parents=True)
    (java_root / "MailController.java").write_text(
        "package example;\nimport example.MailService;\n"
        "public class MailController { public void send() {} }\n",
        encoding="utf-8",
    )
    (java_root / "MailService.java").write_text(
        "package example;\nimport example.MailMapper;\n"
        "public class MailService { public void deliver() {} }\n",
        encoding="utf-8",
    )
    (java_root / "MailMapper.java").write_text(
        "package example;\npublic interface MailMapper {}\n",
        encoding="utf-8",
    )

    result = RepositoryGraph(tmp_path).query("MailService")

    assert "MailService.java" in result
    assert "type:MailService@3" in result
    assert "referenced_by: src/main/java/example/MailController.java" in result


def test_graph_exposes_structured_candidates_and_confidence(tmp_path):
    (tmp_path / "billing.py").write_text(
        "def calculate_invoice():\n    return 1\n",
        encoding="utf-8",
    )

    evidence = RepositoryGraph(tmp_path).inspect("calculate_invoice")

    assert evidence.confidence == "high"
    assert evidence.paths == ("billing.py",)
    assert evidence.matches[0].node.symbols[0].column == 4


def test_repository_intelligence_merges_semantic_locations(tmp_path):
    (tmp_path / "service.py").write_text(
        "def deliver_mail():\n    return True\n",
        encoding="utf-8",
    )

    class FakeSemanticBackend:
        name = "fake-lsp"

        def inspect(self, graph):
            assert graph.paths == ("service.py",)
            return SemanticEvidence(
                self.name,
                "ok",
                (SemanticLocation("reference", "controller.py", 8, 5),),
            )

        def close(self):
            return None

    bundle = RepositoryIntelligence(
        tmp_path,
        semantic_backend=FakeSemanticBackend(),
    ).inspect("deliver_mail")

    assert bundle.confidence == "high"
    assert bundle.paths == ("service.py", "controller.py")
    assert "semantic_index: backend=fake-lsp status=ok" in bundle.render()
    assert "reference: controller.py:8:5" in bundle.render()


def test_language_server_startup_timeout_fails_open_without_hanging():
    class HangingContext:
        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *_args):
            return None

    class Handler:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    class LanguageServer:
        def __init__(self):
            self.server = Handler()

        def start_server(self):
            return HangingContext()

    class Server:
        def __init__(self):
            self.language_server = LanguageServer()
            self.timeout = 0.02
            self.loop = None
            self.loop_thread = None

    server = Server()
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="startup timed out"):
        MultilspySemanticBackend._start_server(server)

    assert time.monotonic() - started < 1
    assert server.language_server.server.stopped is True


def test_implementation_prompt_receives_bounded_repository_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mail_service.py").write_text(
        "class MailService:\n    def schedule(self):\n        pass\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    client = FakeModelClient(["<final>Inspected.</final>"])
    agent = Pico(
        model_client=client,
        workspace=WorkspaceContext.build(workspace, repo_root_override=workspace),
        session_store=SessionStore(state / "sessions"),
        state_root=state,
        approval_policy="auto",
        commit_policy="auto",
    )

    agent.ask("Implement mail scheduling across the service.")

    assert "Repository navigation evidence" in client.prompts[-1]
    assert "class:MailService@1" in client.prompts[-1]
