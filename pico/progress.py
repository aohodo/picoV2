"""Execution evidence and advisory progress reporting.

Tool permissions and execution budgets are enforced at their respective
boundaries. This ledger describes progress; it does not decide the model's
next action or replace fresh tool feedback.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from .read_observation import (
    DEFAULT_SOURCE_WINDOW_LINES,
    ranges_cover,
    visible_read_coverage,
)
from .verification_feedback import verification_observation

NEW_EVIDENCE = "NEW_EVIDENCE"
MATERIAL_PROGRESS = "MATERIAL_PROGRESS"
NO_PROGRESS = "NO_PROGRESS"
INTERVENTION_NORMAL = "NORMAL"
INTERVENTION_SOFT = "SOFT_INTERVENTION"
INTERVENTION_FORCED = "FORCED_DECISION"
STABLE_READ_TOOLS = frozenset({"list_files", "read_file", "read_files", "search", "inspect_repository"})
DISCOVERY_TOOLS = frozenset({"list_files", "read_file", "read_files", "search", "inspect_repository", "delegate"})
VALIDATION_COMMAND = re.compile(
    r"(?i)(^|[;&|]\s*|\s)(pytest|python\s+-m\s+pytest|mvn(?:\s+[^;&|]+)?\s+test|"
    r"gradle\w*\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|make\s+test)(\s|$)"
)
SHELL_SEGMENT = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
REPOSITORY_READ_COMMANDS = frozenset(
    {
        "cat",
        "cd",
        "dir",
        "echo",
        "find",
        "findstr",
        "gc",
        "get-childitem",
        "get-content",
        "grep",
        "head",
        "ls",
        "more",
        "popd",
        "pushd",
        "rg",
        "set-location",
        "tail",
        "type",
    }
)
MAX_LEDGER_OBSERVED_FILES = 48
MAX_LEDGER_RANGES_PER_FILE = 8
MAX_LEDGER_DIRECTORIES = 24
MAX_LEDGER_SEARCHES = 24
MAX_LEDGER_MUTATIONS = 24
MAX_LEDGER_VALIDATIONS = 12
MAX_LEDGER_FAILURES = 12
MAX_LEDGER_UNVERIFIED_CHANGES = 48
MAX_LEDGER_GROUNDING_PATHS = 24
MAX_LEDGER_PATH_REVISIONS = 256


def is_validation_command(command):
    return bool(VALIDATION_COMMAND.search(str(command or "")))


def is_repository_read_command(command):
    """Recognize shell commands containing a typed repository-read operation."""
    text = str(command or "").strip()
    if not text or is_validation_command(text):
        return False
    segments = [item.strip() for item in SHELL_SEGMENT.split(text) if item.strip()]
    if not segments:
        return False
    commands = []
    for segment in segments:
        match = re.match(r"(?i)^(?:&\s*)?([\w.-]+)", segment)
        if match is None:
            return False
        commands.append(match.group(1).casefold())
    # A mixed pipeline is still an opaque evidence channel: `cat x | tool`
    # cannot attach file/revision/range identity to what the model observes.
    # Executable tests/builds were excluded above and remain shell-capable.
    return any(command in REPOSITORY_READ_COMMANDS for command in commands)


def is_repository_read_argv(argv):
    """Recognize a direct-process invocation that only inspects repository data."""
    if not isinstance(argv, list) or not argv:
        return False
    executable = str(argv[0]).strip().replace("\\", "/").rsplit("/", 1)[-1]
    return executable.casefold() in REPOSITORY_READ_COMMANDS


@dataclass(frozen=True)
class ActionSignature:
    tool_name: str
    canonical_args: str
    revision: str

    @classmethod
    def create(cls, tool_name, args, revision):
        canonical = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(str(tool_name), canonical, str(revision))

    @property
    def key(self):
        return f"{self.tool_name}:{self.canonical_args}@{self.revision}"


@dataclass(frozen=True)
class ProgressEvidence:
    kind: str
    observation_hash: str
    reason: str


@dataclass
class ExecutionLedger:
    definition_candidates: list = field(default_factory=list)
    path_revisions: dict = field(default_factory=dict)
    observed_files: dict = field(default_factory=dict)
    observed_directories: set = field(default_factory=set)
    searches: set = field(default_factory=set)
    mutations: list = field(default_factory=list)
    validations: list = field(default_factory=list)
    unresolved_failures: list = field(default_factory=list)
    unverified_changes: set = field(default_factory=set)
    grounding_paths: set = field(default_factory=set)
    grounding_confidence: str = "none"
    semantic_backend: str = "none"
    semantic_status: str = "not_requested"
    broad_exploration_count: int = 0
    targeted_read_count: int = 0
    file_read_count: int = 0
    observed_file_count: int = 0
    observed_directory_count: int = 0
    search_count: int = 0
    mutation_path_count: int = 0
    validation_count: int = 0
    unresolved_failure_count: int = 0
    unverified_change_count: int = 0
    grounding_path_count: int = 0
    frontier_reduction_count: int = 0
    evidence_expansion_count: int = 0
    no_progress_count: int = 0

    def path_revision(self, path):
        return int(self.path_revisions.get(str(path), 0))

    def mark_mutation(self, paths):
        for path in paths:
            key = str(path)
            self.observed_files.pop(key, None)
            self.definition_candidates = [item for item in self.definition_candidates if item["path"] != key]
            # Reinsert updated revisions so the persisted bounded map retains
            # the most recently touched paths across resume cycles.
            previous_revision = self.path_revision(key)
            self.path_revisions.pop(key, None)
            self.path_revisions[key] = previous_revision + 1
            self.mutations.append(key)
            self.mutation_path_count += 1
            if len(self.mutations) > MAX_LEDGER_MUTATIONS:
                del self.mutations[:-MAX_LEDGER_MUTATIONS]
            if key not in self.unverified_changes:
                self.unverified_change_count += 1
            self.unverified_changes.add(key)
            if len(self.unverified_changes) > MAX_LEDGER_UNVERIFIED_CHANGES:
                self.unverified_changes.discard(min(self.unverified_changes))

    def record_read(self, tool_name, args, coverage=None):
        if coverage is not None:
            items = [item for item in coverage if item.get("delivered")]
        elif tool_name == "read_file":
            items = [args]
        elif tool_name == "read_files":
            items = args.get("files", [])
        else:
            items = []
        for item in items:
            path = str(item.get("path", ""))
            self.file_read_count += 1
            if path in self.grounding_paths:
                self.targeted_read_count += 1
            coverage = (int(item.get("start", 1)), int(item.get("end", 0)))
            if path not in self.observed_files:
                self.observed_file_count += 1
            ranges = self.observed_files.setdefault(path, [])
            if coverage not in ranges:
                ranges.append(coverage)
                del ranges[:-MAX_LEDGER_RANGES_PER_FILE]
            while len(self.observed_files) > MAX_LEDGER_OBSERVED_FILES:
                del self.observed_files[next(iter(self.observed_files))]
        if tool_name == "list_files":
            path = str(args.get("path", "."))
            if path not in self.observed_directories:
                self.observed_directory_count += 1
            self.observed_directories.add(path)
            while len(self.observed_directories) > MAX_LEDGER_DIRECTORIES:
                self.observed_directories.discard(min(self.observed_directories))
        elif tool_name == "search":
            search = (str(args.get("pattern", "")), str(args.get("path", ".")))
            if search not in self.searches:
                self.search_count += 1
            self.searches.add(search)
            while len(self.searches) > MAX_LEDGER_SEARCHES:
                self.searches.discard(min(self.searches))
        elif tool_name == "inspect_repository":
            search = (str(args.get("query", "")), "<repository-graph>")
            if search not in self.searches:
                self.search_count += 1
            self.searches.add(search)
            while len(self.searches) > MAX_LEDGER_SEARCHES:
                self.searches.discard(min(self.searches))

    def seed_repository_evidence(self, evidence):
        evidence = evidence or {}
        candidates = {(item["path"], item["start"], item.get("symbol", "")): item
                      for item in self.definition_candidates}
        for item in evidence.get("definition_candidates", []):
            key = (item["path"], item["start"], item.get("symbol", ""))
            candidates.pop(key, None)
            candidates[key] = item
        self.definition_candidates = list(candidates.values())[-MAX_LEDGER_GROUNDING_PATHS:]
        paths = {
            str(path) for path in evidence.get("paths", []) if str(path).strip()
        }
        self.grounding_path_count = len(paths)
        self.grounding_paths = set(sorted(paths)[-MAX_LEDGER_GROUNDING_PATHS:])
        self.grounding_confidence = str(evidence.get("confidence", "none"))
        self.semantic_backend = str(evidence.get("semantic_backend", "none"))
        self.semantic_status = str(evidence.get("semantic_status", "not_requested"))

    def view(self):
        observed_items = list(self.observed_files.items())[-MAX_LEDGER_OBSERVED_FILES:]
        return {
            "pending_definition_candidates": [
                item for item in self.definition_candidates
                if not any(start <= item["start"] <= end
                           for start, end in self.observed_files.get(item["path"], []))
            ],
            "observed_files": {
                path: [list(item) for item in ranges[-MAX_LEDGER_RANGES_PER_FILE:]]
                for path, ranges in sorted(observed_items)
            },
            "observed_file_count": max(self.observed_file_count, len(self.observed_files)),
            "observed_directories": sorted(self.observed_directories)[-MAX_LEDGER_DIRECTORIES:],
            "observed_directory_count": max(
                self.observed_directory_count, len(self.observed_directories)
            ),
            "searches": [
                list(item) for item in sorted(self.searches)[-MAX_LEDGER_SEARCHES:]
            ],
            "search_count": max(self.search_count, len(self.searches)),
            "mutations": list(self.mutations[-MAX_LEDGER_MUTATIONS:]),
            "mutation_path_count": max(self.mutation_path_count, len(self.mutations)),
            "validations": list(self.validations[-MAX_LEDGER_VALIDATIONS:]),
            "validation_count": max(self.validation_count, len(self.validations)),
            "unresolved_failures": [
                {**item, "observation": verification_observation(item.get("output", "")), **({
                    "identity_warning": (
                        "Legacy verification did not save argument boundaries. "
                        "Its display command cannot prove which check failed; "
                        "review original execution evidence before restarting the work unit."
                    ),
                } if "argv" not in item else {})}
                for item in self.unresolved_failures[-MAX_LEDGER_FAILURES:]
            ],
            "unresolved_failure_count": max(
                self.unresolved_failure_count, len(self.unresolved_failures)
            ),
            "unverified_changes": sorted(self.unverified_changes)[
                -MAX_LEDGER_UNVERIFIED_CHANGES:
            ],
            "unverified_change_count": max(
                self.unverified_change_count, len(self.unverified_changes)
            ),
            "grounding_paths": sorted(self.grounding_paths)[-MAX_LEDGER_GROUNDING_PATHS:],
            "grounding_path_count": max(self.grounding_path_count, len(self.grounding_paths)),
            "grounding_confidence": self.grounding_confidence,
            "semantic_backend": self.semantic_backend,
            "semantic_status": self.semantic_status,
            "broad_exploration_count": self.broad_exploration_count,
            "targeted_read_count": self.targeted_read_count,
            "file_read_count": self.file_read_count,
            "frontier_reduction_count": self.frontier_reduction_count,
            "evidence_expansion_count": self.evidence_expansion_count,
            "no_progress_count": self.no_progress_count,
        }

    def to_dict(self):
        recent_revisions = list(self.path_revisions.items())[-MAX_LEDGER_PATH_REVISIONS:]
        return {"path_revisions": dict(recent_revisions),
                "definition_candidates": self.definition_candidates, **self.view(),
                # Failure identity is delivery authority, not a display sample.
                # Keep it until this exact verification succeeds. The model
                # projection in view() remains bounded independently.
                "unresolved_failures": list(self.unresolved_failures)}

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        ledger = cls()
        ledger.definition_candidates = list(data.get("definition_candidates", []))[:MAX_LEDGER_GROUNDING_PATHS]
        ledger.path_revisions = {
            str(k): int(v)
            for k, v in list(data.get("path_revisions", {}).items())[
                -MAX_LEDGER_PATH_REVISIONS:
            ]
        }
        ledger.observed_files = {
            str(path): [tuple(item) for item in ranges[-MAX_LEDGER_RANGES_PER_FILE:]]
            for path, ranges in list(data.get("observed_files", {}).items())[
                -MAX_LEDGER_OBSERVED_FILES:
            ]
        }
        ledger.observed_directories = set(
            data.get("observed_directories", [])[-MAX_LEDGER_DIRECTORIES:]
        )
        ledger.searches = {
            tuple(item) for item in data.get("searches", [])[-MAX_LEDGER_SEARCHES:]
        }
        ledger.mutations = list(data.get("mutations", [])[-MAX_LEDGER_MUTATIONS:])
        ledger.validations = list(data.get("validations", [])[-MAX_LEDGER_VALIDATIONS:])
        ledger.unresolved_failures = list(data.get("unresolved_failures", []))
        ledger.unverified_changes = set(
            data.get("unverified_changes", [])[-MAX_LEDGER_UNVERIFIED_CHANGES:]
        )
        ledger.grounding_paths = set(
            data.get("grounding_paths", [])[-MAX_LEDGER_GROUNDING_PATHS:]
        )
        ledger.grounding_confidence = str(data.get("grounding_confidence", "none"))
        ledger.semantic_backend = str(data.get("semantic_backend", "none"))
        ledger.semantic_status = str(data.get("semantic_status", "not_requested"))
        ledger.broad_exploration_count = int(data.get("broad_exploration_count", 0))
        ledger.targeted_read_count = int(data.get("targeted_read_count", 0))
        ledger.file_read_count = int(data.get("file_read_count", 0))
        ledger.frontier_reduction_count = int(
            data.get("frontier_reduction_count", 0)
        )
        ledger.evidence_expansion_count = int(
            data.get("evidence_expansion_count", 0)
        )
        ledger.no_progress_count = int(data.get("no_progress_count", 0))
        ledger.observed_file_count = max(
            len(ledger.observed_files), int(data.get("observed_file_count", 0))
        )
        ledger.observed_directory_count = max(
            len(ledger.observed_directories),
            int(data.get("observed_directory_count", 0)),
        )
        ledger.search_count = max(len(ledger.searches), int(data.get("search_count", 0)))
        ledger.mutation_path_count = max(
            len(ledger.mutations), int(data.get("mutation_path_count", 0))
        )
        ledger.validation_count = max(
            len(ledger.validations), int(data.get("validation_count", 0))
        )
        ledger.unresolved_failure_count = max(
            len(ledger.unresolved_failures),
            int(data.get("unresolved_failure_count", 0)),
        )
        ledger.unverified_change_count = max(
            len(ledger.unverified_changes),
            int(data.get("unverified_change_count", 0)),
        )
        ledger.grounding_path_count = max(
            len(ledger.grounding_paths), int(data.get("grounding_path_count", 0))
        )
        return ledger


@dataclass
class ProgressState:
    workspace_revision: int = 0
    discovery_streak: int = 0
    steps_since_material_progress: int = 0
    no_progress_streak: int = 0
    blocked_repeats: int = 0
    intervention_level: str = INTERVENTION_NORMAL
    intervention_count: int = 0
    max_discovery_streak: int = 0
    mutation_count: int = 0
    shell_count: int = 0
    stuck_detected: bool = False
    post_hard_no_progress: int = 0
    successful_reads: set = field(default_factory=set)
    observations: set = field(default_factory=set)
    pending_notices: list = field(default_factory=list)
    rejected_actions: set = field(default_factory=set)
    delivery_review_pending: bool = False
    delivery_review_presented: bool = False
    delivery_review_completed: bool = False
    last_action: dict = field(default_factory=dict)
    evidence_expansion_streak: int = 0


class ProgressController:
    def __init__(
        self,
        max_steps,
        read_only=False,
        soft_discovery_limit=None,
        hard_discovery_limit=None,
        ledger=None,
        repository_evidence=None,
        delivery_requirements=None,
    ):
        self.max_steps = max(1, int(max_steps))
        self.read_only = bool(read_only)
        # Accept legacy configuration for compatibility, but do not turn
        # exploration counts into tool restrictions or task termination.
        self.soft_discovery_limit = soft_discovery_limit
        self.hard_discovery_limit = hard_discovery_limit
        if soft_discovery_limit is not None and int(soft_discovery_limit) < 1:
            raise ValueError("soft_discovery_limit must be positive")
        if hard_discovery_limit is not None and int(hard_discovery_limit) < 1:
            raise ValueError("hard_discovery_limit must be positive")
        if (soft_discovery_limit is not None and hard_discovery_limit is not None
                and int(hard_discovery_limit) < int(soft_discovery_limit)):
            raise ValueError("hard_discovery_limit must be >= soft_discovery_limit")
        self.state = ProgressState()
        self.remaining_steps = self.max_steps
        self._read_contents = {}
        self._visible_outputs = None
        self._visible_reads = None
        self._prefetched_reads = []
        self.delivery_requirements = dict(delivery_requirements or {})
        self.ledger = ExecutionLedger.from_dict(ledger)
        validation_records = [
            item for item in self.ledger.validations
            if item.get("kind") == "validation"
        ]
        self.state.delivery_review_pending = bool(
            self.ledger.mutation_path_count
            and validation_records
            and not self.ledger.unresolved_failures
            and not self.ledger.unresolved_failure_count
            and not self.ledger.unverified_changes
            and not self.ledger.unverified_change_count
        )
        if repository_evidence:
            self.ledger.seed_repository_evidence(repository_evidence)

    @staticmethod
    def _is_broad_exploration(tool_name, args):
        return tool_name in {"list_files", "search"} and str(args.get("path", ".")) in {"", "."}

    def _revision_for(self, tool_name, args):
        if tool_name == "read_file":
            path = str(args.get("path", ""))
            return f"{path}:{self.ledger.path_revision(path)}"
        if tool_name == "read_files":
            return "|".join(
                f"{item.get('path', '')}:{self.ledger.path_revision(item.get('path', ''))}"
                for item in args.get("files", [])
            )
        return str(self.state.workspace_revision)

    def signature(self, tool_name, args):
        return ActionSignature.create(tool_name, args, self._revision_for(tool_name, args))

    def _requested_existing_paths(self):
        return [
            str(path)
            for path in self.delivery_requirements.get(
                "requested_existing_paths", ()
            )
            if str(path).strip()
        ]

    def _requested_missing_paths(self):
        return [
            str(path)
            for path in self.delivery_requirements.get(
                "requested_missing_paths", ()
            )
            if str(path).strip()
        ]

    def _unread_requested_paths(self):
        modified = set(self.ledger.mutations)
        return [
            path
            for path in self._requested_existing_paths()
            if path not in self.ledger.observed_files and path not in modified
        ]

    def _unread_candidate_paths(self):
        requested = set(self._requested_existing_paths())
        modified = set(self.ledger.mutations)
        return [
            path
            for path in sorted(self.ledger.grounding_paths)
            if path not in requested
            and path not in self.ledger.observed_files
            and path not in modified
        ]

    def _evidence_frontier(self):
        frontier = {
            ("requested_path", path) for path in self._unread_requested_paths()
        }
        frontier.update(
            ("candidate_path", path) for path in self._unread_candidate_paths()
        )
        frontier.update(
            (
                "definition",
                str(item.get("path", "")),
                int(item.get("start", 0)),
            )
            for item in self.ledger.view()["pending_definition_candidates"]
        )
        return frontier

    @staticmethod
    def _action_targets(tool_name, args, metadata, read_evidence):
        targets = []
        if read_evidence:
            targets.extend(str(item.get("path", "")) for item in read_evidence)
        elif tool_name == "read_file" and args.get("path"):
            targets.append(str(args["path"]))
        elif tool_name == "read_files":
            targets.extend(
                str(item.get("path", "")) for item in args.get("files", ())
            )
        targets.extend(str(path) for path in metadata.get("affected_paths", ()))
        return list(dict.fromkeys(path for path in targets if path))

    def work_focus_view(self):
        """Project one shared decision context from otherwise separate ledgers.

        This view is advisory.  It does not hide tools, impose a discovery
        quota, or claim that reading a file means its behavior is understood.
        Its job is to keep the current evidence frontier visible after history
        compaction so the model can make the next decision from facts instead
        of reconstructing the task state on every turn.
        """
        unread = self._unread_requested_paths()
        unread_candidates = self._unread_candidate_paths()
        observed_requested = [
            path
            for path in self._requested_existing_paths()
            if path in self.ledger.observed_files
        ]
        pending_definitions = self.ledger.view()["pending_definition_candidates"]
        failures_present = bool(
            self.ledger.unresolved_failures
            or self.ledger.unresolved_failure_count
        )
        unverified = sorted(self.ledger.unverified_changes)

        if failures_present:
            phase = "repair_failure"
            priority = (
                "Interpret the newest unresolved failure first; change or rerun "
                "only what that evidence justifies."
            )
        elif unverified or self.ledger.unverified_change_count:
            phase = "complete_and_verify_changes"
            priority = (
                "Finish the requested change set, then verify the current revision; "
                "do not restart repository-wide discovery."
            )
        elif self.state.delivery_review_pending:
            phase = "delivery_review"
            priority = "Review the verified diff against the original request and finish."
        elif unread:
            phase = "inspect_explicit_targets"
            priority = (
                "Read the named existing targets needed for the requested behavior. "
                "Broaden exploration only for a concrete unresolved dependency."
            )
        elif unread_candidates:
            phase = "inspect_candidate_paths"
            priority = (
                "Inspect the grounded candidate paths before broad repository exploration."
            )
        elif pending_definitions:
            phase = "inspect_candidate_definitions"
            priority = "Inspect the pending definition candidates that bear on the request."
        elif self.read_only:
            phase = "answer_from_evidence"
            priority = (
                "Answer from current evidence, or make one targeted read for a clearly "
                "missing fact."
            )
        elif observed_requested or self.ledger.observed_files:
            phase = "implement"
            priority = (
                "The named targets have source evidence. Implement now unless one concrete "
                "dependency is still unknown."
            )
        else:
            phase = "locate_relevant_code"
            priority = "Locate the smallest source set that can answer the current request."

        return {
            "phase": phase,
            "priority": priority,
            "known": {
                "observed_requested_paths": observed_requested,
                "modified_paths": list(self.ledger.mutations[-8:]),
                "last_action": dict(self.state.last_action),
            },
            "open": {
                "unread_requested_paths": unread,
                "unread_candidate_paths": unread_candidates,
                "named_missing_paths": self._requested_missing_paths(),
                "pending_definition_candidates": pending_definitions[:8],
                "unverified_changes": unverified[-12:],
                "unresolved_failure_count": self.ledger.unresolved_failure_count,
            },
            "action_value_counts": {
                "frontier_reducing": self.ledger.frontier_reduction_count,
                "evidence_expanding": self.ledger.evidence_expansion_count,
                "no_progress": self.ledger.no_progress_count,
            },
            "evidence_expansion_streak": self.state.evidence_expansion_streak,
        }

    def set_visible_tool_outputs(self, events):
        self._visible_outputs = {
            str(item.get("output", "")) for item in events
            if item.get("type") == "function_call_output"
        }
        self._visible_reads = [
            *self._prefetched_reads,
            *[
                record
                for event in events
                if event.get("type") == "function_call_output"
                for record in event.get("_read_evidence", [])
            ],
        ]

    def seed_working_set(self, coverage):
        """Register source that is actually present in the first model input."""
        visible = [
            {
                **item,
                "revision": self.ledger.path_revision(item.get("path", "")),
            }
            for item in coverage
            if item.get("delivered")
        ]
        self._prefetched_reads = visible
        self.ledger.record_read("read_files", {}, visible)
        return visible

    def set_visible_text_context(self, prompt):
        self._visible_outputs = {content for content in self._read_contents.values() if content in prompt}
        self._visible_reads = None

    def _read_is_visible(self, tool_name, args):
        signature = self.signature(tool_name, args)
        if tool_name != "read_file":
            content = self._read_contents.get(signature.key)
            return bool(content) and content in (self._visible_outputs or set())
        if self._visible_reads is None:
            content = self._read_contents.get(signature.key)
            return bool(content) and content in (self._visible_outputs or set())
        items = [args]
        return bool(items) and all(
            ranges_cover(
                self._visible_reads,
                item.get("path", ""),
                self.ledger.path_revision(item.get("path", "")),
                int(item.get("start", 1)),
                int(item.get("end", DEFAULT_SOURCE_WINDOW_LINES)),
            )
            for item in items
        )

    def preflight(self, tool_name, args):
        signature = self.signature(tool_name, args)
        # This is an evidence-identity check, not a read-count quota. If the
        # exact current source range is already present in the model's actual
        # input, executing the read cannot add information. Once the evidence
        # is compacted away or the path revision changes, the read is allowed.
        if tool_name in STABLE_READ_TOOLS and self._read_is_visible(
            tool_name, args
        ):
            self.state.blocked_repeats += 1
            self.state.no_progress_streak += 1
            self.ledger.no_progress_count += 1
            self.state.last_action = {
                "tool": tool_name,
                "value": "no_progress",
                "targets": self._action_targets(tool_name, args, {}, None),
                "reason": "repeated_no_progress",
            }
            self._maybe_intervene()
            return {
                "allowed": False,
                "signature": signature,
                "evidence": ProgressEvidence(
                    NO_PROGRESS, "", "repeated_no_progress"
                ),
            }
        # Broad exploration remains model-directed. Permissions, path
        # boundaries and approval are enforced at their own boundaries.
        return {"allowed": True, "signature": signature}

    def requires_material_action(self):
        return False

    def set_remaining_steps(self, remaining):
        self.remaining_steps = max(0, int(remaining))

    def admissible_tools(self, tool_names):
        # Keep the tool interface stable while the model repairs failed work.
        return set(tool_names)

    @staticmethod
    def action_key(tool_name, args):
        canonical = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{tool_name}:{canonical}"

    def preflight_known_error(self, tool_name, args):
        # Return fresh validation feedback even for a repeated bad request.
        return True

    def record_rejected_action(self, tool_name, args):
        self.state.rejected_actions.add(self.action_key(tool_name, args))

    def observe(self, tool_name, args, content, metadata):
        frontier_before = self._evidence_frontier()
        signature = self.signature(tool_name, args)
        observation_hash = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        identity = f"{signature.key}:{observation_hash}"
        status = str(metadata.get("tool_status", ""))
        executed = bool(metadata.get("executed", False))
        changed = bool(metadata.get("workspace_changed", False))
        if "validation" in metadata:
            authoritative_verification = bool(metadata["validation"])
        else:
            # Backward compatibility for persisted ledgers and callers from
            # before verification purpose was explicit: run_verification was
            # always authoritative unless it explicitly says diagnostic.
            authoritative_verification = bool(
                tool_name == "run_verification"
                and str(args.get("purpose", "acceptance")).lower()
                != "diagnostic"
            )
        read_evidence = None
        if executed and "read_coverage" in metadata:
            read_evidence = visible_read_coverage(content, metadata["read_coverage"])

        if changed:
            affected_paths = list(metadata.get("affected_paths", []))
            if not affected_paths and args.get("path"):
                affected_paths = [str(args["path"])]
            self.ledger.mark_mutation(affected_paths)
            if read_evidence is not None:
                read_evidence = [
                    {**item, "revision": self.ledger.path_revision(item["path"])}
                    for item in read_evidence
                ]
                metadata["read_evidence"] = read_evidence
            evidence = ProgressEvidence(MATERIAL_PROGRESS, observation_hash, "workspace_changed")
            self.state.workspace_revision += 1
            self.state.mutation_count += 1
            self.state.discovery_streak = self.state.steps_since_material_progress = 0
            self.state.intervention_level = INTERVENTION_NORMAL
            self.state.no_progress_streak = self.state.post_hard_no_progress = 0
            self.state.pending_notices.clear()
            self.state.pending_notices.append(
                "Runtime notice: the workspace changed and needs verification. "
                "Complete the related implementation and tests, then use run_verification. "
                "A successful file write is not evidence that the code builds or passes tests. "
                "Reuse known file locations instead of restarting repository exploration."
            )
            self.state.stuck_detected = False
            # Argument validation failures belong to the workspace revision in
            # which they occurred. A successful mutation may make the exact
            # same operation valid, so failures must not poison later phases.
            self.state.rejected_actions.clear()
            # A review only applies to the exact implementation that was
            # validated. Any later mutation reopens both verification and
            # senior review.
            self.state.delivery_review_pending = False
            self.state.delivery_review_presented = False
            self.state.delivery_review_completed = False
        else:
            if read_evidence is not None:
                read_evidence = [
                    {**item, "revision": self.ledger.path_revision(item["path"])}
                    for item in read_evidence
                ]
                metadata["read_evidence"] = read_evidence
            if read_evidence is not None and self._visible_reads is not None:
                novel = any(not ranges_cover(
                    self._visible_reads, item["path"], item["revision"], item["start"], item["end"]
                ) for item in read_evidence)
                historical = all(ranges_cover(
                    [{"path": item["path"], "revision": item["revision"], "start": start, "end": end}
                     for start, end in self.ledger.observed_files.get(item["path"], [])],
                    item["path"], item["revision"], item["start"], item["end"]
                ) for item in read_evidence)
                reason = ("context_restored" if historical else "missing_source_delivered") if novel else "source_already_visible"
                evidence = ProgressEvidence(NEW_EVIDENCE if novel else NO_PROGRESS, observation_hash, reason)
                self.state.no_progress_streak = 0 if novel else self.state.no_progress_streak + 1
            elif (executed and status == "ok" and tool_name in {"read_file", "read_files"}
                  and self._visible_outputs is not None and str(content) not in self._visible_outputs):
                evidence = ProgressEvidence(NEW_EVIDENCE, observation_hash, "context_restored")
                self.state.no_progress_streak = 0
            elif executed and identity not in self.state.observations and status in {"ok", "partial_success", "error"}:
                evidence = ProgressEvidence(NEW_EVIDENCE, observation_hash, "new_observation")
                self.state.no_progress_streak = 0
            else:
                evidence = ProgressEvidence(NO_PROGRESS, observation_hash, "repeated_or_rejected")
                self.state.no_progress_streak += 1
            self.state.steps_since_material_progress += 1
            if tool_name in DISCOVERY_TOOLS:
                self.state.discovery_streak += 1
                self.state.max_discovery_streak = max(self.state.max_discovery_streak, self.state.discovery_streak)
            elif tool_name in {"run_shell", "run_verification"} and executed:
                validation = authoritative_verification or (
                    tool_name == "run_shell" and is_validation_command(args.get("command", ""))
                )
                if validation:
                    self.state.shell_count += 1
                    self.state.discovery_streak = 0
                else:
                    # A shell command without a workspace effect or a test
                    # result is observation, not an implementation change.
                    self.state.discovery_streak += 1
                    self.state.max_discovery_streak = max(
                        self.state.max_discovery_streak,
                        self.state.discovery_streak,
                    )
            # Failed writes do not reset the discovery metrics: only an
            # actual workspace change or verification result does that.

        if executed:
            self.state.observations.add(identity)
            if tool_name in STABLE_READ_TOOLS and status == "ok":
                self.state.successful_reads.add(signature.key)
                self._read_contents[signature.key] = str(content)
                while len(self._read_contents) > MAX_LEDGER_OBSERVED_FILES * MAX_LEDGER_RANGES_PER_FILE:
                    del self._read_contents[next(iter(self._read_contents))]
                self.ledger.record_read(tool_name, args, read_evidence if read_evidence is not None else metadata.get("read_coverage"))
                if self._is_broad_exploration(tool_name, args):
                    self.ledger.broad_exploration_count += 1
                    if (
                        self.ledger.grounding_confidence == "high"
                        and self.ledger.grounding_paths
                        and self.ledger.targeted_read_count == 0
                    ):
                        self.state.pending_notices.append(self._grounding_notice())
            if tool_name in {"run_shell", "run_verification"}:
                validation = authoritative_verification
                command = (
                    " ".join(str(item) for item in args.get("argv", []))
                    if tool_name == "run_verification"
                    else str(args.get("command", ""))
                )
                record = {
                    "command": command,
                    "status": status,
                    "kind": (
                        "validation"
                        if validation
                        else (
                            "diagnostic"
                            if tool_name == "run_verification"
                            else "shell_execution"
                        )
                    ),
                }
                if metadata.get("verification_evidence"):
                    record["verification_evidence"] = dict(
                        metadata["verification_evidence"]
                    )
                if validation:
                    # Display text is lossy (one "a b" argument is not two
                    # arguments). Preserve the executed argument boundaries
                    # for retries and persisted failure resolution.
                    record["argv"] = list(args["argv"])
                    previous = [
                        item for item in self.ledger.unresolved_failures
                        if item.get("argv") == record["argv"]
                    ]
                    remaining = [
                        item for item in self.ledger.unresolved_failures
                        if item.get("argv") != record["argv"]
                    ]
                self.ledger.validations.append(record)
                self.ledger.validation_count += 1
                del self.ledger.validations[:-MAX_LEDGER_VALIDATIONS]
                if validation and not changed:
                    # Even a failed check yields actionable feedback. Reopen the
                    # repair cycle, but never consider that failure verified.
                    self.state.discovery_streak = self.state.steps_since_material_progress = 0
                    self.state.intervention_level = INTERVENTION_NORMAL
                    self.state.post_hard_no_progress = 0
                    self.state.pending_notices.clear()
                    self.state.stuck_detected = False
                if validation and status == "ok" and not changed:
                    self.ledger.unverified_changes.clear()
                    self.ledger.unverified_change_count = 0
                    # Legacy failures without argv cannot be matched safely by
                    # splitting their display command; retain that uncertainty.
                    self.ledger.unresolved_failures = remaining
                    self.ledger.unresolved_failure_count = max(0, self.ledger.unresolved_failure_count - len(previous))
                    if self.ledger.mutation_path_count:
                        self.state.delivery_review_pending = True
                        self.state.delivery_review_presented = False
                        self.state.delivery_review_completed = False
                elif validation:
                    self.ledger.unresolved_failures = remaining
                    # ToolExecutor has already bounded and redacted content.
                    # Keep the latest actionable failure until this check
                    # passes, independently of ordinary history eviction.
                    self.ledger.unresolved_failures.append({**record, "output": str(content)})
                    if not previous:
                        self.ledger.unresolved_failure_count += 1
                    elif len(previous) > 1:
                        self.ledger.unresolved_failure_count -= len(previous) - 1
        frontier_after = self._evidence_frontier()
        targets = self._action_targets(tool_name, args, metadata, read_evidence)
        if changed:
            action_value = "material_change"
            self.ledger.frontier_reduction_count += 1
            self.state.evidence_expansion_streak = 0
        elif authoritative_verification and executed:
            action_value = (
                "verification_passed" if status == "ok" else "failure_feedback"
            )
            if status == "ok":
                self.ledger.frontier_reduction_count += 1
            else:
                self.ledger.evidence_expansion_count += 1
            self.state.evidence_expansion_streak = 0
        elif frontier_after < frontier_before:
            action_value = "frontier_reduced"
            self.ledger.frontier_reduction_count += 1
            self.state.evidence_expansion_streak = 0
        elif evidence.kind == NEW_EVIDENCE:
            action_value = "evidence_expanded"
            self.ledger.evidence_expansion_count += 1
            self.state.evidence_expansion_streak += 1
        else:
            action_value = "no_progress"
            self.ledger.no_progress_count += 1
        self.state.last_action = {
            "tool": tool_name,
            "value": action_value,
            "targets": targets,
            "reason": evidence.reason,
        }
        current_phase = self.work_focus_view()["phase"]
        if (
            action_value == "evidence_expanded"
            and current_phase in {"implement", "complete_and_verify_changes"}
        ):
            self.state.pending_notices.append(
                "Runtime decision feedback: the last action added context but did not "
                "close a known evidence item. If it answered a concrete implementation "
                "question, use that answer now. Before another discovery action, identify "
                "the still-open fact it will decide; otherwise implement or verify the "
                "current work unit. Tools remain available for a genuinely new dependency."
            )
        self._maybe_intervene()
        return evidence

    def delivery_review_notice(self):
        if not self.state.delivery_review_pending:
            return ""
        return (
            "Runtime notice: delivery review is active. Before returning a final answer, "
            "act like a senior maintainer reviewing another developer's change: compare the "
            "original request with the current post-edit mutation receipts, diff evidence, and "
            "verification result already in context. Independence means checking the implementation "
            "against the specification, not fetching the same evidence again. Check each required "
            "behavior, semantic "
            "invariant, boundary case, compatibility promise, architecture constraint, and "
            "modified-file scope against the current implementation. Passing authored tests "
            "are evidence, not the specification or proof of correctness. If a claim is not "
            "supported by current evidence, inspect that concrete gap or correct the implementation "
            "and rerun verification; do not reread or repeat a search solely to confirm an edit "
            "receipt. If the "
            "delivery is sound, finish without inventing more work."
        )

    def mark_delivery_review_presented(self):
        if self.state.delivery_review_pending:
            self.state.delivery_review_presented = True

    def complete_delivery_review(self):
        if self.state.delivery_review_pending and self.state.delivery_review_presented:
            self.state.delivery_review_pending = False
            self.state.delivery_review_completed = True

    def _maybe_intervene(self):
        if (
            self.state.intervention_level == INTERVENTION_NORMAL
            and self.state.no_progress_streak >= 2
        ):
            self.state.intervention_level = INTERVENTION_SOFT
            self.state.intervention_count += 1
            self.state.pending_notices.append(self._soft_notice())

    def consume_notice(self):
        return self.state.pending_notices.pop(0) if self.state.pending_notices else ""

    def runtime_state_view(self):
        return {
            "work_focus": self.work_focus_view(),
            "progress": self.metrics(),
            "ledger": self.ledger.view(),
        }

    def _soft_notice(self):
        if self.read_only:
            action = "choose one genuinely new target, finalize, or identify a blocker."
        else:
            action = "choose one genuinely new target, modify, verify, finalize, or identify a blocker."
        return "Runtime notice: Recent actions are not advancing the task. Reuse prior observations and " + action

    def _grounding_notice(self):
        candidates = ", ".join(sorted(self.ledger.grounding_paths)[:6])
        return (
            "Runtime notice: repository evidence already identified high-confidence candidate files "
            f"({candidates}). Read those targets before another repository-wide listing or search."
        )

    def metrics(self):
        validation_records = [
            item
            for item in self.ledger.validations
            if item.get("kind") == "validation"
        ]
        if self.ledger.unresolved_failure_count or self.ledger.unresolved_failures:
            validation_status = "failed"
        elif validation_records and (self.ledger.unverified_change_count or self.ledger.unverified_changes):
            validation_status = "stale"
        elif validation_records:
            validation_status = "passed"
        elif self.ledger.unverified_change_count or self.ledger.unverified_changes:
            validation_status = "not_run"
        else:
            validation_status = "not_required"
        return {
            "workspace_revision": self.state.workspace_revision,
            "blocked_repeats": self.state.blocked_repeats,
            "intervention_count": self.state.intervention_count,
            "max_discovery_streak": self.state.max_discovery_streak,
            "stuck_detected": self.state.stuck_detected,
            "validation_status": validation_status,
            "discovery_streak": self.state.discovery_streak,
            "no_progress_streak": self.state.no_progress_streak,
            "intervention_level": self.state.intervention_level,
            "initial_evidence_count": len(self.ledger.grounding_paths),
            "grounding_confidence": self.ledger.grounding_confidence,
            "semantic_backend": self.ledger.semantic_backend,
            "semantic_status": self.ledger.semantic_status,
            "broad_exploration_count": self.ledger.broad_exploration_count,
            "targeted_read_count": self.ledger.targeted_read_count,
            "evidence_hit_rate": (
                self.ledger.targeted_read_count / self.ledger.file_read_count
                if self.ledger.file_read_count
                else 0.0
            ),
            "frontier_reduction_count": self.ledger.frontier_reduction_count,
            "evidence_expansion_count": self.ledger.evidence_expansion_count,
            "no_progress_count": self.ledger.no_progress_count,
            "evidence_expansion_streak": self.state.evidence_expansion_streak,
            "delivery_review_status": (
                "completed"
                if self.state.delivery_review_completed
                else "presented"
                if self.state.delivery_review_presented
                else "pending"
                if self.state.delivery_review_pending
                else "not_required"
            ),
        }
