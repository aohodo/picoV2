"""Bounded, dependency-aware repository navigation for Python and Java."""

from __future__ import annotations

import ast
import os
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
    r"(?m)^[ \t]*(?:[\w.$<>?,\[\]]+[ \t]+)+"
    r"(\w+)[ \t]*\([^;{}]*\)[ \t]*(?:throws[ \t]+[^{;]+)?\{"
)


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line: int
    column: int = 0
    end_line: int = 0
    calls: tuple[str, ...] = ()


@dataclass
class SourceNode:
    path: str
    language: str
    module: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: str = ""


@dataclass(frozen=True)
class RankedSourceNode:
    score: int
    node: SourceNode


@dataclass(frozen=True)
class RepositoryGraphEvidence:
    query: str
    total_files: int
    truncated: bool
    matches: tuple[RankedSourceNode, ...]
    reverse: dict[str, frozenset[str]] = field(default_factory=dict, compare=False, repr=False)
    definitions: tuple[tuple[str, str, int, int], ...] = ()

    def relevant_symbols(self, node):
        tokens = RepositoryGraph._query_tokens(self.query)
        return sorted(node.symbols, key=lambda s: (
            -sum(2 if token == s.name.casefold() else 1
                 for token in tokens if token in s.name.casefold()), s.line
        ))[:20]

    @property
    def paths(self):
        return tuple(item.node.path for item in self.matches)

    @property
    def confidence(self):
        if not self.matches:
            return "none"
        score = self.matches[0].score
        if score >= 12:
            return "high"
        if score >= 6:
            return "medium"
        return "low"

    def render(self):
        lines = [
            (
                "repository_graph: "
                f"files={self.total_files} truncated={'yes' if self.truncated else 'no'} "
                f"confidence={self.confidence} query={self.query!r}"
            )
        ]
        if self.definitions:
            lines.append("definition candidates (name matches, not proven dynamic dispatch; read these ranges):")
            for name, path, start, end in self.definitions:
                lines.append(f"  {name}: {path}:{start}-{end}")
        for item in self.matches:
            score, node = item.score, item.node
            symbols = ", ".join(
                f"{symbol.kind}:{symbol.name}@{symbol.line}" for symbol in self.relevant_symbols(node)
            ) or "none"
            imports = ", ".join(node.imports[:12]) or "none"
            inbound = ", ".join(sorted(self.reverse.get(node.path, ()))[:12]) or "none"
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


class RepositoryGraph:
    """Build a small evidence graph without requiring a language server."""

    def __init__(self, root, max_files=MAX_SOURCE_FILES):
        self.root = Path(root).resolve()
        self.max_files = int(max_files)

    def query(self, query, limit=12):
        return self.inspect(query, limit=limit).render()

    def inspect(self, query, limit=12):
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
        # Follow calls only from symbols explicitly named by the query. A file
        # match alone does not justify expanding every function in that file.
        called_names = set()
        for _, node in selected:
            for symbol in node.symbols:
                if symbol.name.casefold() in tokens:
                    called_names.update(symbol.calls)
        definitions = []
        for node in nodes:
            for symbol in node.symbols:
                if symbol.name in called_names or symbol.name.casefold() in tokens:
                    definitions.append((symbol.name, node.path, symbol.line,
                                        symbol.end_line or symbol.line))
        return RepositoryGraphEvidence(
            query=query,
            total_files=len(nodes),
            truncated=truncated,
            matches=tuple(RankedSourceNode(score, node) for score, node in selected),
            reverse={path: frozenset(items) for path, items in reverse.items()},
            definitions=tuple(definitions[:max(1, min(int(limit), 30))]),
        )

    def _build(self):
        paths = []
        truncated = False
        excluded = IGNORED_PATH_NAMES | {"target", "build", "dist", "node_modules"}
        for directory, directories, files in os.walk(self.root, followlinks=False):
            directories[:] = sorted(
                (name for name in directories if name not in excluded
                 and not (Path(directory) / name).is_symlink()), key=str.casefold
            )
            for name in sorted(files, key=str.casefold):
                path = Path(directory) / name
                if path.suffix.casefold() not in SOURCE_SUFFIXES:
                    continue
                if len(paths) >= self.max_files:
                    truncated = True
                    break
                try:
                    path.resolve().relative_to(self.root)
                    if path.stat().st_size <= MAX_SOURCE_BYTES:
                        paths.append(path)
                except (OSError, ValueError):
                    continue
            if truncated:
                break
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
        source_lines = text.splitlines()
        for item in ast.walk(tree):
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(item, ast.ClassDef) else "function"
                line = source_lines[int(item.lineno) - 1]
                column = line.find(item.name, int(item.col_offset))
                node.symbols.append(
                    Symbol(
                        item.name,
                        kind,
                        int(item.lineno),
                        column if column >= 0 else int(item.col_offset),
                        int(item.end_lineno or item.lineno),
                        tuple(sorted({
                            call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
                            for call in ast.walk(item)
                            if isinstance(call, ast.Call)
                            and isinstance(call.func, (ast.Name, ast.Attribute))
                        })) if kind == "function" else (),
                    )
                )
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
        # Blank comments and literals while preserving positions/line numbers.
        syntax = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                        lambda match: re.sub(r'[^\n]', ' ', match[0]), text)
        for match in JAVA_TYPE.finditer(text):
            line_start = text.rfind("\n", 0, match.start(1)) + 1
            node.symbols.append(
                Symbol(
                    match.group(1),
                    "type",
                    text.count("\n", 0, match.start(1)) + 1,
                    match.start(1) - line_start,
                )
            )
        for match in JAVA_METHOD.finditer(syntax):
            name = match.group(1)
            if name not in {"if", "for", "while", "switch", "catch", "return", "new"}:
                line_start = text.rfind("\n", 0, match.start(1)) + 1
                opening = match.end() - 1
                depth, closing = 1, opening + 1
                while closing < len(syntax) and depth:
                    depth += (syntax[closing] == "{") - (syntax[closing] == "}")
                    closing += 1
                calls = tuple(sorted(set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", syntax[opening + 1:closing]))
                                     - {"if", "for", "while", "switch", "catch", "synchronized"}))
                node.symbols.append(
                    Symbol(
                        name,
                        "method",
                        text.count("\n", 0, match.start(1)) + 1,
                        match.start(1) - line_start,
                        text.count("\n", 0, closing) + 1,
                        calls,
                    )
                )
        node.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
        return node

    @staticmethod
    def _query_tokens(query):
        tokens = {token.casefold() for token in TOKEN_PATTERN.findall(query) if len(token) > 1}
        tokens.update(token.rsplit(".", 1)[-1] for token in tuple(tokens) if "." in token)
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
            # Reverse dependencies are supporting evidence, not a popularity
            # contest. Without a cap, generic words such as "service" make a
            # widely inherited base class outrank the named policy/symbol.
            score += min(
                4,
                sum(2 for name in reverse.get(node.path, ()) if token in name.casefold()),
            )
        return score
