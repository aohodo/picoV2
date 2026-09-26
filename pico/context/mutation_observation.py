"""Post-edit workspace evidence for mutation tools."""

from __future__ import annotations

import difflib

from .read_observation import ReadObservation, render_reads


def _changed_ranges(before_lines, after_lines, line_limit):
    """Return bounded post-edit ranges around actual changed hunks."""
    opcodes = difflib.SequenceMatcher(a=before_lines, b=after_lines).get_opcodes()
    ranges = []
    for tag, _old_start, _old_end, new_start, new_end in opcodes:
        if tag == "equal":
            continue
        start = max(1, new_start + 1)
        end = max(start, new_end)
        if after_lines:
            start = min(start, len(after_lines))
            end = min(max(start, end), len(after_lines))
        ranges.append((start, end))
    if not ranges and after_lines:
        ranges = [(1, min(len(after_lines), line_limit))]

    # Allocate the viewer's line window across distinct hunks. This keeps
    # edit feedback semantic (whole numbered lines) instead of slicing at an
    # arbitrary character offset.
    remaining = max(1, int(line_limit))
    bounded = []
    for index, (start, end) in enumerate(ranges):
        share = max(1, remaining // (len(ranges) - index))
        changed_count = end - start + 1
        if changed_count >= share:
            head = max(1, share // 2)
            tail = share - head
            bounded.append((start, start + head - 1))
            if tail and end - tail + 1 > start + head - 1:
                bounded.append((end - tail + 1, end))
            remaining -= share
            continue
        context = share - changed_count
        window_start = max(1, start - context // 2)
        window_end = min(len(after_lines), end + context - context // 2)
        bounded.append((window_start, window_end))
        remaining -= window_end - window_start + 1
    # Context allocated independently around nearby hunks can overlap. Merge
    # those windows before rendering so the observation budget is spent on
    # distinct current source rather than duplicate numbered lines.
    merged = []
    for start, end in sorted(bounded):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _bounded_diff(before_lines, after_lines, path, char_budget):
    lines = list(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=3,
    ))
    if not lines or char_budget <= 0:
        return ""
    selected = []
    used = 0
    for line in lines:
        size = len(line) + 1
        if used + size > char_budget:
            marker = f"... [diff omitted {len(lines) - len(selected)} lines]"
            if used + len(marker) <= char_budget:
                selected.append(marker)
            break
        selected.append(line)
        used += size
    return "\n".join(selected)


def render_mutation_observation(
    path,
    before_text,
    after_document,
    operation,
    char_budget,
    line_limit,
):
    """Describe the committed shadow edit and expose its current source view."""
    before_lines = str(before_text or "").splitlines()
    after_lines = after_document.text.splitlines()
    ranges = _changed_ranges(before_lines, after_lines, line_limit)
    header = (
        f"mutation_applied: {operation}\n"
        f"path: {path}\n"
        "state: current post-edit workspace content; a confirmation read is unnecessary "
        "unless another change makes this evidence stale\n"
    )
    # Prefer a complete post-edit snapshot whenever it naturally fits the
    # existing observation budget. A senior reviewer reasons about the whole
    # current unit, not only the lines that happened to change. The diff is
    # secondary because the complete current source already proves what will
    # be delivered.
    full_diff_placeholder = "diff: (omitted; complete current source follows)\n"
    full_source = render_reads(
        [(path, after_document.encoding, after_lines, 1, len(after_lines))],
        max(1, int(char_budget) - len(header) - len(full_diff_placeholder)),
    ) if after_lines else None
    if (
        full_source is not None
        and full_source.coverage
        and full_source.coverage[0].get("delivered")
        and not full_source.coverage[0].get("truncated")
    ):
        diff_budget = max(
            0,
            int(char_budget)
            - len(header)
            - len(str(full_source))
            - len("diff:\n\n"),
        )
        diff = _bounded_diff(before_lines, after_lines, path, diff_budget)
        diff_section = (
            f"diff:\n{diff}\n"
            if diff
            else full_diff_placeholder
        )
        return ReadObservation(
            header + diff_section + str(full_source), full_source.coverage
        )

    diff_budget = max(0, (int(char_budget) - len(header)) // 3)
    diff = _bounded_diff(before_lines, after_lines, path, diff_budget)
    diff_section = f"diff:\n{diff}\n" if diff else "diff: (no textual difference)\n"
    source_budget = max(1, int(char_budget) - len(header) - len(diff_section))
    documents = [
        (path, after_document.encoding, after_lines, start, end)
        for start, end in ranges
    ]
    source = render_reads(documents, source_budget) if documents else ReadObservation(
        f"# {path} [current file is empty]", []
    )
    return ReadObservation(header + diff_section + str(source), source.coverage)
