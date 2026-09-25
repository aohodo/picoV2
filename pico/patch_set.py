"""Plan and apply one atomic set of exact repository edits."""

from __future__ import annotations

from dataclasses import dataclass

from .mutation_observation import render_mutation_observation
from .path_support import native_path
from .read_observation import ReadObservation, render_reads
from .text_document import TextDocument, read_text_document, write_text_document


class PatchSetError(ValueError):
    """A rejected patch set carrying current source for the repair turn."""

    code = "patch_match_failed"

    def __init__(self, message, observation):
        super().__init__(f"{message}\n{observation}")
        self.coverage = observation.coverage


@dataclass(frozen=True)
class PlannedFilePatch:
    path: object
    relative_path: str
    document: TextDocument
    updated_text: str


def _consistent_line_ending(text):
    """Return the file's line ending when it has one consistent representation."""
    without_crlf = text.replace("\r\n", "")
    endings = []
    if "\r\n" in text:
        endings.append("\r\n")
    if "\n" in without_crlf:
        endings.append("\n")
    if "\r" in without_crlf:
        endings.append("\r")
    return endings[0] if len(endings) == 1 else None


def _use_line_ending(text, line_ending):
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return canonical.replace("\n", line_ending)


def adapt_edit_to_document(document_text, old_text, new_text):
    """Map protocol newlines to a consistently encoded repository document.

    Source observations are line based, so their transport representation uses
    ``\n`` even when the file stores CRLF. Treating that representation detail
    as source content made an otherwise exact multi-line patch impossible. The
    character content remains exact; only newline representation is adapted.
    Mixed-newline files deliberately receive no adaptation.
    """
    line_ending = _consistent_line_ending(document_text)
    if line_ending is None:
        return old_text, new_text
    adapted_old = (
        _use_line_ending(old_text, line_ending)
        if "\n" in old_text or "\r" in old_text
        else old_text
    )
    adapted_new = (
        _use_line_ending(new_text, line_ending)
        if "\n" in new_text or "\r" in new_text
        else new_text
    )
    return adapted_old, adapted_new


def _match_failure(context, relative_path, document, old_text, edit_number):
    text = document.text
    lines = text.splitlines()
    count = text.count(old_text)
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
        import difflib

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
        [(relative_path, document.encoding, lines, start, end)],
        context.observation_char_budget,
    )
    return PatchSetError(
        f"edit {edit_number} for {relative_path}: {detail}. The entire patch set was "
        "rejected before writing. Construct a corrected exact edit from the current "
        "source below; do not reread solely to recover this patch.",
        observation,
    )


def plan_patch_set(context, edits):
    """Validate every edit against one source snapshot without changing files."""
    grouped = {}
    order = []
    for edit_number, edit in enumerate(edits, start=1):
        path = context.path(edit["path"])
        relative = path.relative_to(context.root).as_posix()
        if relative not in grouped:
            grouped[relative] = {
                "path": path,
                "document": read_text_document(path),
                "edits": [],
            }
            order.append(relative)
        grouped[relative]["edits"].append((edit_number, edit))

    plans = []
    for relative in order:
        item = grouped[relative]
        document = item["document"]
        spans = []
        for edit_number, edit in item["edits"]:
            old_text, new_text = adapt_edit_to_document(
                document.text,
                str(edit["old_text"]),
                str(edit["new_text"]),
            )
            if document.text.count(old_text) != 1:
                raise _match_failure(
                    context, relative, document, old_text, edit_number
                )
            start = document.text.index(old_text)
            end = start + len(old_text)
            if any(start < other_end and other_start < end for other_start, other_end, _ in spans):
                observation = render_reads(
                    [(
                        relative,
                        document.encoding,
                        document.text.splitlines(),
                        max(1, document.text.count("\n", 0, start) + 1),
                        min(
                            len(document.text.splitlines()),
                            document.text.count("\n", 0, end)
                            + context.source_window_lines,
                        ),
                    )],
                    context.observation_char_budget,
                )
                raise PatchSetError(
                    f"edit {edit_number} for {relative} overlaps another edit. The entire "
                    "patch set was rejected before writing.",
                    observation,
                )
            spans.append((start, end, new_text))

        updated = document.text
        for start, end, new_text in sorted(spans, reverse=True):
            updated = updated[:start] + new_text + updated[end:]
        # Prove every result is encodable before the first file is touched.
        document.to_bytes(updated)
        plans.append(PlannedFilePatch(item["path"], relative, document, updated))
    return plans


def commit_patch_set(plans):
    """Apply prepared files and restore earlier files if a later write fails."""
    attempted = []
    try:
        for plan in plans:
            # Include the current target before writing: a filesystem error can
            # be raised after a file was truncated or partially replaced.
            attempted.append(plan)
            write_text_document(plan.path, plan.updated_text, plan.document)
    except BaseException:
        rollback_errors = []
        for plan in reversed(attempted):
            try:
                native_path(plan.path).write_bytes(plan.document.to_bytes())
            except OSError as rollback_error:
                rollback_errors.append(f"{plan.relative_path}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "patch set write failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise


def render_patch_set_observation(context, plans):
    """Return bounded post-edit evidence for every file in the work unit."""
    header = (
        f"mutation_applied: apply_patch\nfiles: {len(plans)}\n"
        "state: all edits were validated before writing and applied as one work unit; "
        "confirmation reads are unnecessary unless another change makes this evidence stale\n"
    )
    remaining = max(1, context.observation_char_budget - len(header))
    rendered = []
    coverage = []
    for index, plan in enumerate(plans):
        files_left = len(plans) - index
        file_budget = max(1, remaining // files_left)
        current = read_text_document(plan.path)
        observation = render_mutation_observation(
            plan.relative_path,
            plan.document.text,
            current,
            "apply_patch",
            file_budget,
            context.source_window_lines,
        )
        section = str(observation)
        rendered.append(section)
        coverage.extend(observation.coverage)
        remaining = max(1, remaining - len(section) - 2)
    return ReadObservation(header + "\n\n".join(rendered), coverage)
