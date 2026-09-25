"""Build a bounded first-turn source working set from user-named files."""

import re
from dataclasses import dataclass

from .read_observation import (
    DEFAULT_OBSERVATION_CHAR_BUDGET,
    DEFAULT_SOURCE_WINDOW_LINES,
    render_reads,
    retain_read_evidence,
)
from .text_document import TextDecodingError, read_text_document

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_IMPORT = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|package\b|using\b|require\s*\()"
)
_QUERY_STOP_WORDS = frozenset(
    {
        "and",
        "create",
        "every",
        "exactly",
        "existing",
        "extract",
        "file",
        "files",
        "from",
        "into",
        "modify",
        "only",
        "preserve",
        "project",
        "repository",
        "rule",
        "run",
        "same",
        "source",
        "style",
        "that",
        "then",
        "this",
        "three",
        "update",
        "use",
        "with",
    }
)


@dataclass(frozen=True)
class InitialWorkingSet:
    text: str
    coverage: tuple[dict, ...]


def project_current_working_set(text, coverage, path_revision):
    """Keep only prefetched source that still matches the workspace revision."""
    delivered = [item for item in coverage if item.get("delivered")]
    current = [
        item
        for item in delivered
        if int(item.get("revision", 0))
        == int(path_revision(item.get("path", "")))
    ]
    if not current:
        return ""
    if len(current) == len(delivered):
        return str(text or "")
    return retain_read_evidence(text, current)


def _query_terms(query):
    return tuple(
        dict.fromkeys(
            token.casefold()
            for token in _WORD.findall(str(query or ""))
            if token.casefold() not in _QUERY_STOP_WORDS
        )
    )


def _merge_ranges(ranges, line_count):
    merged = []
    for start, end in sorted(ranges):
        start = max(1, int(start))
        end = min(line_count, int(end))
        if end < start:
            continue
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _relevant_ranges(lines, query):
    if not lines:
        return []
    ranges = [(1, min(len(lines), DEFAULT_SOURCE_WINDOW_LINES))]
    import_lines = [
        index for index, line in enumerate(lines, 1) if _IMPORT.search(line)
    ]
    if import_lines:
        ranges.append((import_lines[0], import_lines[-1]))

    terms = _query_terms(query)
    scored = []
    for index, line in enumerate(lines, 1):
        folded = line.casefold()
        score = sum(len(term) for term in terms if term in folded)
        if score:
            scored.append((score, index))
    if scored:
        _, best_line = max(scored)
        context = max(1, DEFAULT_SOURCE_WINDOW_LINES // 20)
        ranges.append((best_line - context, best_line + context))
    return _merge_ranges(ranges, len(lines))


def build_initial_working_set(
    path_resolver,
    query,
    paths,
    budget=DEFAULT_OBSERVATION_CHAR_BUDGET,
):
    """Return source excerpts for concrete existing paths within one read budget."""
    documents = []
    for logical_path in dict.fromkeys(str(path) for path in paths):
        try:
            document = read_text_document(path_resolver(logical_path))
        except (OSError, TextDecodingError, ValueError):
            continue
        lines = document.text.splitlines()
        for start, end in _relevant_ranges(lines, query):
            documents.append(
                (logical_path, document.encoding, lines, start, end)
            )
    if not documents:
        return InitialWorkingSet("", ())
    rendered = render_reads(documents, int(budget))
    coverage = tuple(item for item in rendered.coverage if item.get("delivered"))
    return InitialWorkingSet(str(rendered), coverage)
