"""Layer fast repository indexing with optional language-server semantics."""

from __future__ import annotations

import asyncio
import atexit
import gzip
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from .repository_graph import RepositoryGraph, RepositoryGraphEvidence


class _StartedLanguageServer:
    """Own a multilspy event loop and guarantee bounded shutdown."""

    def __init__(self, server, async_context, loop_thread):
        self.server = server
        self.async_context = async_context
        self.loop_thread = loop_thread

    def close(self):
        loop = self.server.loop
        if loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            self.async_context.__aexit__(None, None, None), loop
        )
        try:
            future.result(timeout=10)
        except Exception:  # noqa: BLE001 - force cleanup after an unhealthy LSP
            future.cancel()
            _stop_language_server(self.server, timeout=5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            self.loop_thread.join(timeout=5)


def _stop_language_server(server, timeout):
    """Stop the LSP process tree without trusting its protocol shutdown path."""
    loop = server.loop
    if loop is None or loop.is_closed():
        return
    handler = server.language_server.server
    future = asyncio.run_coroutine_threadsafe(handler.stop(), loop)
    try:
        future.result(timeout=timeout)
    except Exception:  # noqa: BLE001 - best-effort process-tree cleanup
        future.cancel()


@dataclass(frozen=True)
class SemanticLocation:
    relation: str
    path: str
    line: int
    column: int


@dataclass(frozen=True)
class SemanticEvidence:
    backend: str
    status: str
    locations: tuple[SemanticLocation, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class RepositoryEvidenceBundle:
    graph: RepositoryGraphEvidence
    semantic: SemanticEvidence = field(
        default_factory=lambda: SemanticEvidence("none", "not_requested")
    )

    @property
    def paths(self):
        paths = list(self.graph.paths)
        for _, path, _, _ in self.graph.definitions:
            if path not in paths:
                paths.append(path)
        for location in self.semantic.locations:
            if location.path not in paths:
                paths.append(location.path)
        return tuple(paths)

    @property
    def confidence(self):
        if self.semantic.locations:
            return "high"
        return self.graph.confidence

    def ledger_seed(self):
        return {
            "paths": list(self.paths),
            "confidence": self.confidence,
            "semantic_backend": self.semantic.backend,
            "semantic_status": self.semantic.status,
            "definition_candidates": [
                {"symbol": name, "path": path, "start": start, "end": end}
                for name, path, start, end in self.graph.definitions
            ] + [
                {"symbol": "", "path": item.path, "start": item.line, "end": item.line}
                for item in self.semantic.locations if item.relation == "definition"
            ],
        }

    def render(self):
        lines = [self.graph.render()]
        lines.append(
            f"semantic_index: backend={self.semantic.backend} status={self.semantic.status}"
        )
        if self.semantic.detail:
            lines.append(f"  detail: {self.semantic.detail}")
        for location in self.semantic.locations:
            lines.append(
                f"  {location.relation}: {location.path}:{location.line}:{location.column}"
            )
        return "\n".join(lines)


class MultilspySemanticBackend:
    """Lazy Python/Java LSP adapter backed by Microsoft's multilspy.

    The dependency and language-server processes are deliberately optional.
    A missing or unhealthy server degrades to RepositoryGraph rather than
    making repository navigation or Pico startup fail.
    """

    name = "multilspy"
    supported_languages = frozenset({"python", "java"})
    _setup_lock = threading.Lock()

    def __init__(self, root, cache_root=None):
        self.root = Path(root).resolve()
        self.cache_root = Path(cache_root).resolve() if cache_root is not None else None
        self._servers = {}
        self._contexts = {}
        self._closed = False
        atexit.register(self.close)

    @staticmethod
    def _start_server(server):
        """Start multilspy with a deadline that also covers initialization.

        multilspy applies ``timeout`` to requests but its synchronous
        ``start_server`` waits forever. Drive the async context directly so a
        broken language server cannot block the agent runtime.
        """
        server.loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=server.loop.run_forever, daemon=True)
        server.loop_thread = loop_thread
        loop_thread.start()
        async_context = server.language_server.start_server()
        future = asyncio.run_coroutine_threadsafe(
            async_context.__aenter__(), server.loop
        )
        try:
            future.result(timeout=server.timeout or 30)
        except FutureTimeoutError as exc:
            future.cancel()
            _stop_language_server(server, timeout=5)
            server.loop.call_soon_threadsafe(server.loop.stop)
            loop_thread.join(timeout=5)
            raise TimeoutError("language server startup timed out") from exc
        except Exception:
            future.cancel()
            _stop_language_server(server, timeout=5)
            server.loop.call_soon_threadsafe(server.loop.stop)
            loop_thread.join(timeout=5)
            raise
        return _StartedLanguageServer(server, async_context, loop_thread)

    @staticmethod
    def installed():
        return importlib.util.find_spec("multilspy") is not None

    def _server(self, language):
        if language in self._servers:
            return self._servers[language]
        import multilspy
        from multilspy import SyncLanguageServer
        from multilspy.multilspy_config import MultilspyConfig
        from multilspy.multilspy_logger import MultilspyLogger
        from multilspy.multilspy_settings import MultilspySettings
        from multilspy.multilspy_utils import FileUtils

        if self.cache_root is None:
            raise RuntimeError("semantic cache root is not configured")
        lsp_root = self.cache_root / "lsp"
        global_cache = self.cache_root / "global_cache"
        lsp_root.mkdir(parents=True, exist_ok=True)
        global_cache.mkdir(parents=True, exist_ok=True)
        # multilspy 0.0.15 otherwise hardcodes Path.home()/.multilspy.
        # Bind both locations to Pico's host-owned state root before creating
        # a server so language binaries, indexes, and JDT workspaces never
        # leak into the user's home drive.
        MultilspySettings.get_language_server_directory = staticmethod(
            lambda: str(lsp_root)
        )
        MultilspySettings.get_global_cache_directory = staticmethod(
            lambda: str(global_cache)
        )

        config = MultilspyConfig.from_dict({"code_language": language})
        package_root = Path(multilspy.__file__).resolve().parent.parent
        if os.name == "nt" and language == "java" and package_root.drive != self.cache_root.drive:
            raise RuntimeError(
                "Java language-server package and semantic cache must be installed on the same drive"
            )
        with self._setup_lock:
            original_download = FileUtils.download_and_extract_archive
            FileUtils.download_and_extract_archive = staticmethod(
                self._download_and_extract
            )
            try:
                server = SyncLanguageServer.create(
                    config,
                    MultilspyLogger(),
                    str(self.root),
                    timeout=15,
                )
            finally:
                FileUtils.download_and_extract_archive = staticmethod(original_download)
        self._configure_server_process(server, language, package_root)
        context = self._start_server(server)
        self._servers[language] = server
        self._contexts[language] = context
        return server

    def _download_and_extract(self, _logger, url, target_path, archive_type):
        """Mirror multilspy's downloader while keeping all temporary bytes on the state drive."""
        download_root = self.cache_root / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        archive = download_root / uuid.uuid4().hex
        expanded = None
        try:
            with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            if archive_type in {"zip", "tar", "gztar", "bztar", "xztar"}:
                shutil.unpack_archive(str(archive), str(target_path), archive_type)
            elif archive_type == "zip.gz":
                expanded = archive.with_suffix(".zip")
                with gzip.open(archive, "rb") as source, expanded.open("wb") as target:
                    shutil.copyfileobj(source, target)
                shutil.unpack_archive(str(expanded), str(target_path), "zip")
            elif archive_type == "gz":
                with gzip.open(archive, "rb") as source, Path(target_path).open("wb") as target:
                    shutil.copyfileobj(source, target)
            else:
                raise RuntimeError(f"unsupported language-server archive type: {archive_type}")
        finally:
            archive.unlink(missing_ok=True)
            if expanded is not None:
                expanded.unlink(missing_ok=True)

    def _configure_server_process(self, server, language, package_root):
        process = server.language_server.server.process_launch_info
        if language == "python":
            wrapper_root = self.cache_root / "bin"
            wrapper_root.mkdir(parents=True, exist_ok=True)
            wrapper = wrapper_root / "pico_jedi_language_server.py"
            wrapper.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(package_root)!r})\n"
                "import jedi.settings\n"
                f"jedi.settings.cache_directory = {str(self.cache_root / 'jedi-cache')!r}\n"
                "from jedi_language_server.cli import cli\n"
                "cli()\n",
                encoding="utf-8",
            )
            argv = [sys.executable, str(wrapper)]
            process.cmd = (
                subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
            )
        elif language == "java":
            java_home = self.cache_root / "java-home"
            java_home.mkdir(parents=True, exist_ok=True)
            marker = "-Dfile.encoding=utf8"
            process.cmd = process.cmd.replace(
                marker,
                f'-Duser.home="{java_home}" {marker}',
                1,
            )

    @staticmethod
    def _anchors(graph):
        tokens = RepositoryGraph._query_tokens(graph.query)
        anchors = []
        for match in graph.matches:
            ranked = sorted(
                match.node.symbols,
                key=lambda symbol: (
                    0 if any(token in symbol.name.casefold() for token in tokens) else 1,
                    symbol.line,
                    symbol.name,
                ),
            )
            for symbol in ranked[:1]:
                anchors.append((match.node.language, match.node.path, symbol))
            if len(anchors) >= 4:
                break
        return anchors

    def _relative_location(self, relation, item):
        if not isinstance(item, dict):
            return None
        raw_uri = str(item.get("uri") or item.get("targetUri") or "")
        if not raw_uri:
            return None
        parsed = urlparse(raw_uri)
        raw_path = unquote(parsed.path) if parsed.scheme == "file" else raw_uri
        if parsed.scheme == "file" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        try:
            relative = Path(raw_path).resolve().relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return None
        span = item.get("range") or item.get("targetSelectionRange") or {}
        start = span.get("start") or {}
        return SemanticLocation(
            relation=relation,
            path=relative,
            line=int(start.get("line", 0)) + 1,
            column=int(start.get("character", 0)) + 1,
        )

    @staticmethod
    def _as_items(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        return []

    def inspect(self, graph):
        if not self.installed():
            return SemanticEvidence(self.name, "unavailable", detail="install pico[lsp]")
        if self.cache_root is None:
            return SemanticEvidence(
                self.name,
                "unavailable",
                detail="semantic cache root is not configured",
            )
        locations = []
        failures = []
        failed_languages = set()
        for language, path, symbol in self._anchors(graph):
            if language not in self.supported_languages or language in failed_languages:
                continue
            try:
                server = self._server(language)
                definitions = server.request_definition(
                    path, max(0, symbol.line - 1), max(0, symbol.column)
                )
                references = server.request_references(
                    path, max(0, symbol.line - 1), max(0, symbol.column)
                )
                for relation, result in (("definition", definitions), ("reference", references)):
                    for item in self._as_items(result):
                        location = self._relative_location(relation, item)
                        if location is not None and location not in locations:
                            locations.append(location)
            except Exception as exc:  # noqa: BLE001 - optional semantic service must fail open
                failed_languages.add(language)
                detail = str(exc).replace("\r", " ").replace("\n", " ")[:240]
                failures.append(
                    f"{language}:{type(exc).__name__}{f':{detail}' if detail else ''}"
                )
        status = "ok" if locations else ("error" if failures else "no_results")
        return SemanticEvidence(
            self.name,
            status,
            tuple(locations[:80]),
            detail=", ".join(sorted(set(failures))) if failures else "",
        )

    def close(self):
        if self._closed:
            return
        for language, context in reversed(list(self._contexts.items())):
            try:
                context.close()
            except Exception:  # noqa: BLE001,S110 - process cleanup is best effort
                pass
            finally:
                self._contexts.pop(language, None)
                self._servers.pop(language, None)
        self._closed = True
        atexit.unregister(self.close)


class RepositoryIntelligence:
    def __init__(
        self,
        root,
        semantic_backend=None,
        semantic_mode="auto",
        semantic_cache_root=None,
    ):
        self.root = Path(root).resolve()
        self.graph = RepositoryGraph(self.root)
        mode = str(semantic_mode or "auto").strip().lower()
        if mode not in {"auto", "off"}:
            raise ValueError("semantic_mode must be 'auto' or 'off'")
        if semantic_backend is not None:
            self.semantic_backend = semantic_backend
        elif mode == "off":
            self.semantic_backend = None
        else:
            self.semantic_backend = MultilspySemanticBackend(
                self.root,
                cache_root=semantic_cache_root,
            )

    def inspect(self, query, limit=12, include_semantic=True):
        graph = self.graph.inspect(query, limit=limit)
        semantic = SemanticEvidence("none", "disabled")
        if include_semantic and self.semantic_backend is not None:
            semantic = self.semantic_backend.inspect(graph)
        elif self.semantic_backend is not None:
            semantic = SemanticEvidence(self.semantic_backend.name, "not_requested")
        return RepositoryEvidenceBundle(graph, semantic)

    def close(self):
        if self.semantic_backend is not None:
            self.semantic_backend.close()
