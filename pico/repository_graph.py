"""Bounded, dependency-aware repository navigation for Python and Java."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from .text_document import TextDecodingError, read_text_document
from .workspace import IGNORED_PATH_NAMES

SOURCE_SUFFIXES = frozenset({".py", ".java"})
MAX_SOURCE_FILES = 800
MAX_SOURCE_BYTES = 512_000
TOKEN_PATTERN = re.compile(r"[\w.$/-]+", re.UNICODE)
JAVA_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
JAVA_IMPORT = re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.*]+)\s*;")
JAVA_TYPE = re.compile(
    r"(?m)^\s*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+|static\s+)*"
    r"(?:class|interface|enum|record)\s+(\w+)"
)
JAVA_METHOD = re.compile(
    r"(?m)^\s*(?:@[\w.]+(?:\([^\n]*\))?\s*)*"
    r"(?:public|protected|private|static|final|synchronized|abstract|native|default|strictfp|\s)+"
    r"[\w.$<>?,\[\]\s]+\s+(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{"
)


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line: int


@dataclass
class SourceNode:
    path: str
    language: str
    module: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: str = ""


class RepositoryGraph:
    """Build a small evidence graph without requiring a language server."""

    def __init__(self, root, max_files=MAX_SOURCE_FILES):
        self.root = Path(root).resolve()
        self.max_files = int(max_files)

    def query(self, query, limit=12):
        query = str(query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        nodes, truncated = self._build()
        internal_names = self._internal_names(nodes)
        reverse = self._reverse_dependencies(nodes, internal_names)
        tokens = self._query_tokens(query)
        ranked = sorted(
            ((self._score(node, tokens, reverse), node) for node in nodes),
            key=lambda item: (-item[0], item[1].path.casefold()),
        )
        selected = [(score, node) for score, node in ranked if score > 0][: max(1, min(int(limit), 30))]
        if not selected:
            selected = ranked[: max(1, min(int(limit), 30))]
        lines = [
            f"repository_graph: files={len(nodes)} truncated={'yes' if truncated else 'no'} query={query!r}"
        ]
        for score, node in selected:
            symbols = ", ".join(
                f"{symbol.kind}:{symbol.name}@{symbol.line}" for symbol in node.symbols[:20]
            ) or "none"
            imports = ", ".join(node.imports[:12]) or "none"
            inbound = ", ".join(sorted(reverse.get(node.path, ()))[:12]) or "none"
            lines.extend(
                [
                    f"- {node.path} [language={node.language} score={score}]",
                    f"  module: {node.module or 'none'}",
                    f"  symbols: {symbols}",
                    f"  imports: {imports}",
                    f"  referenced_by: {inbound}",
                ]
            )
            if node.parse_error:
                lines.append(f"  parse_error: {node.parse_error}")
        return "\n".join(lines)

    def _build(self):
        paths = []
        truncated = False
        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file() or path.suffix.casefold() not in SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(self.root)
            if any(part in IGNORED_PATH_NAMES or part in {"target", "build", "dist", "node_modules"} for part in relative.parts):
                continue
            if len(paths) >= self.max_files:
                truncated = True
                break
            try:
                if path.stat().st_size <= MAX_SOURCE_BYTES:
                    paths.append(path)
            except OSError:
                continue
        nodes = []
        for path in paths:
            try:
                text = read_text_document(path).text
            except (OSError, TextDecodingError) as exc:
                nodes.append(
                    SourceNode(
                        path=path.relative_to(self.root).as_posix(),
                        language="python" if path.suffix.casefold() == ".py" else "java",
                        parse_error=str(exc),
                    )
                )
                continue
            nodes.append(self._parse_python(path, text) if path.suffix.casefold() == ".py" else self._parse_java(path, text))
        return nodes, truncated

    def _parse_python(self, path, text):
        relative = path.relative_to(self.root).as_posix()
        module = relative.removesuffix(".py").replace("/", ".")
        module = module.removesuffix(".__init__")
        node = SourceNode(path=relative, language="python", module=module)
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError as exc:
            node.parse_error = f"{exc.msg} at line {exc.lineno}"
            return node
        for item in ast.walk(tree):
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(item, ast.ClassDef) else "function"
                node.symbols.append(Symbol(item.name, kind, int(item.lineno)))
            elif isinstance(item, ast.Import):
                node.imports.extend(alias.name for alias in item.names)
            elif isinstance(item, ast.ImportFrom):
                prefix = "." * int(item.level) + str(item.module or "")
                node.imports.append(prefix)
        node.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
        node.imports = sorted(set(node.imports))
        return node

    def _parse_java(self, path, text):
        relative = path.relative_to(self.root).as_posix()
        package_match = JAVA_PACKAGE.search(text)
        package = package_match.group(1) if package_match else ""
        node = SourceNode(path=relative, language="java", module=package)
        node.imports = sorted(set(JAVA_IMPORT.findall(text)))
        for match in JAVA_TYPE.finditer(text):
            node.symbols.append(Symbol(match.group(1), "type", text.count("\n", 0, match.start()) + 1))
        for match in JAVA_METHOD.finditer(text):
            name = match.group(1)
            if name not in {"if", "for", "while", "switch", "catch", "return", "new"}:
                node.symbols.append(Symbol(name, "method", text.count("\n", 0, match.start()) + 1))
        node.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
        return node

    @staticmethod
    def _query_tokens(query):
        tokens = {token.casefold() for token in TOKEN_PATTERN.findall(query) if len(token) > 1}
        return tokens or {query.casefold()}

    @staticmethod
    def _internal_names(nodes):
        names = {}
        for node in nodes:
            names[node.path.removesuffix(".py").replace("/", ".")] = node.path
            if node.language == "java":
                for symbol in node.symbols:
                    if symbol.kind == "type":
                        qualified = f"{node.module}.{symbol.name}" if node.module else symbol.name
                        names[qualified] = node.path
        return names

    @staticmethod
    def _resolve_import(import_name, internal_names):
        normalized = import_name.lstrip(".").removesuffix(".*")
        if normalized in internal_names:
            return internal_names[normalized]
        candidates = [
            path
            for name, path in internal_names.items()
            if name.startswith(normalized + ".") or name.endswith("." + normalized)
        ]
        return min(candidates) if candidates else ""

    def _reverse_dependencies(self, nodes, internal_names):
        reverse = {node.path: set() for node in nodes}
        for node in nodes:
            for import_name in node.imports:
                target = self._resolve_import(import_name, internal_names)
                if target and target != node.path:
                    reverse.setdefault(target, set()).add(node.path)
        return reverse

    @staticmethod
    def _score(node, tokens, reverse):
        path = node.path.casefold()
        module = node.module.casefold()
        symbol_names = [symbol.name.casefold() for symbol in node.symbols]
        imports = [name.casefold() for name in node.imports]
        score = 0
        for token in tokens:
            if token in path:
                score += 8
            if token in module:
                score += 6
            score += sum(12 for name in symbol_names if token in name)
            score += sum(3 for name in imports if token in name)
            score += sum(2 for name in reverse.get(node.path, ()) if token in name.casefold())
        return score
