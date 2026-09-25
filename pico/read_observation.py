"""Bounded file observations with coverage measured after rendering."""

import ast
import re

DEFAULT_SOURCE_WINDOW_LINES = 100
DEFAULT_OBSERVATION_CHAR_BUDGET = 8_000


def visible_read_coverage(text, coverage):
    """Intersect rendered numbered bodies with trusted delivered ranges.

    Locations and summaries are not source evidence. Numbered source lines
    cannot introduce a unit marker because the renderer prefixes each line.
    """
    visible = []
    active = None
    for line in str(text).splitlines():
        marker = re.fullmatch(r"# unit: (.+):(\d+)-(\d+) .*\[source fragment\]", line)
        if line.startswith(("#", "[")):
            active = marker
            continue
        number = re.match(r"^\s*(\d+): ", line)
        if active is None or number is None:
            continue
        index = int(number[1])
        for item in coverage:
            if (item.get("delivered") and item["path"] == active[1]
                    and item["start"] <= index <= item["end"]
                    and int(active[2]) <= index <= int(active[3])):
                record = {**item, "start": index, "end": index}
                if (visible and visible[-1]["path"] == record["path"]
                        and visible[-1].get("revision") == record.get("revision")
                        and visible[-1]["end"] + 1 == index):
                    visible[-1]["end"] = index
                else:
                    visible.append(record)
                break
    return visible


def retain_read_evidence(text, coverage):
    """Render only source lines backed by the supplied evidence records.

    A single ``read_files`` result may contain independently versioned files.
    When one file changes, keeping the unchanged units is safer than dropping
    the whole tool result.  Reconstructing from numbered source lines also
    prevents a stale neighbouring file header from being mistaken for source.
    """
    retained = []
    active = None
    for line in str(text).splitlines():
        marker = re.fullmatch(
            r"# unit: (.+):(\d+)-(\d+) .*(\[source fragment\])", line
        )
        if marker is not None:
            path = marker[1]
            unit_start = int(marker[2])
            unit_end = int(marker[3])
            matching = [
                item
                for item in coverage
                if item.get("delivered")
                and item.get("path") == path
                and item.get("start", 1) <= unit_end
                and item.get("end", 0) >= unit_start
            ]
            active = (path, matching)
            if matching:
                retained.append(line)
            continue
        number = re.match(r"^\s*(\d+): ", line)
        if number is not None and active is not None:
            index = int(number[1])
            if any(
                item.get("start", 1) <= index <= item.get("end", 0)
                for item in active[1]
            ):
                retained.append(line)
            continue
        # Excerpt notices belong to the active unit. File headers and mutation
        # receipt/diff prose do not prove source freshness and are omitted.
        if active is not None and active[1] and line.startswith("["):
            retained.append(line)
    return "\n".join(retained)


def ranges_cover(coverage, path, revision, start, end):
    """Whether the union of current source ranges covers a nonempty request."""
    if end < start:
        return False
    cursor = start
    for item in sorted(coverage, key=lambda item: item["start"]):
        if item["path"] != path or item.get("revision", 0) != revision:
            continue
        if item["start"] > cursor:
            break
        cursor = max(cursor, item["end"] + 1)
        if cursor > end:
            return True
    return False


def compact_read_observation(text, budget):
    """Keep whole source fragments or explicit locations, never splice their code."""
    if len(text) <= budget:
        return text
    units = re.split(r"(?m)(?=^# unit: )", str(text))
    units = [unit.strip() for unit in units if unit.startswith("# unit: ")]
    notice = "[compacted source evidence; omitted bodies are not available in this context]\n"
    if not units:
        return "[source excerpt omitted; use the call's file and range to retrieve it]"[
            :budget
        ]
    output = notice
    for index, unit in enumerate(units):
        share = max(0, (budget - len(output)) // (len(units) - index))
        if len(unit) + 1 <= share:
            output += unit + "\n"
        else:
            location = unit.splitlines()[0] + " [body omitted; read this range]\n"
            if len(location) <= share:
                output += location
    return output[:budget]


def enclosing_symbols(path, lines, start, end):
    """Label excerpt ownership from syntax, rather than asking the model to guess."""
    if not path.endswith(".py"):
        return ""
    try:
        tree = ast.parse("\n".join(lines))
    except (SyntaxError, ValueError, RecursionError):
        return ""
    names = []

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}.{child.name}" if prefix else child.name
                if child.lineno <= end and (child.end_lineno or child.lineno) >= start:
                    if not isinstance(child, ast.ClassDef):
                        names.append(f"{qualified}:{child.lineno}-{child.end_lineno}")
                    visit(child, qualified)
            else:
                visit(child, prefix)

    visit(tree)
    return "; ".join(names)


class ReadObservation(str):
    """Remain compatible with text tools while carrying delivered evidence."""

    def __new__(cls, text, coverage):
        result = super().__new__(cls, text)
        result.coverage = coverage
        return result


def render_reads(documents, budget):
    """Give every requested file a share; count only complete delivered lines."""
    sections = []
    coverage = []
    remaining = budget - 2 * max(0, len(documents) - 1)
    for index, (path, encoding, lines, start, end) in enumerate(documents):
        share = max(0, remaining // (len(documents) - index))
        header = f"# {path} [encoding={encoding}; total_lines={len(lines)}]\n"
        symbols = enclosing_symbols(path, lines, start, start)
        if symbols:
            # Ownership is navigation metadata; the numbered body is evidence.
            label = f"# enclosing symbol (body may be partial): {symbols}\n"
            if len(label) <= share // 4:
                header += label
        footer = (
            "\n[excerpt only; use read_file with a narrower line range to continue]"
        )
        available = max(0, share - len(header) - len(footer))
        body = []
        used = 0
        # One marker per requested excerpt, not one repeated path per AST
        # declaration/blank line. Navigation metadata must not crowd out code.
        marker = f"# unit: {path}:{start}-{min(end, len(lines))} source [source fragment]"
        delivered_lines = 0
        for number in range(start, min(end, len(lines)) + 1):
            line = f"{number:>4}: {lines[number - 1]}"
            if number == start:
                line = marker + "\n" + line
            size = len(line) + bool(body)
            if used + size > available:
                break
            body.append(line)
            used += size
            delivered_lines += 1
        delivered_end = start + delivered_lines - 1
        # Unit locations describe actual output, never the unreturned suffix.
        body = [
            re.sub(
                r"(?m)^(# unit: .+:\d+)-(\d+) ",
                lambda match, last=delivered_end: (
                    f"{match[1]}-{min(int(match[2]), last)} "
                ),
                item,
            )
            for item in body
        ]
        truncated = delivered_end < min(end, len(lines))
        text = header + "\n".join(body) + (footer if truncated else "")
        # Very long paths still cannot bypass the global output budget.
        if len(text) > share:
            text = "[file omitted: metadata exceeds output budget]"[:share]
            body = []
            delivered_end = start - 1
            truncated = True
        sections.append(text)
        remaining -= len(text)
        coverage.append(
            {
                "path": path,
                "start": start,
                "end": delivered_end,
                "requested_end": end,
                "truncated": truncated,
                "delivered": bool(body),
            }
        )
    return ReadObservation("\n\n".join(sections), coverage)
