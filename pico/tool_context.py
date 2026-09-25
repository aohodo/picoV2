"""Narrow context passed from runtime into tool functions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .read_observation import (
    DEFAULT_OBSERVATION_CHAR_BUDGET,
    DEFAULT_SOURCE_WINDOW_LINES,
)


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    repository_inspector: Callable[[str, int], object] | None = None
    pending_test_paths_provider: Callable[[], list[str]] | None = None
    command_runner: object = None
    observation_char_budget: int = DEFAULT_OBSERVATION_CHAR_BUDGET
    source_window_lines: int = DEFAULT_SOURCE_WINDOW_LINES

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def pending_test_paths(self):
        if self.pending_test_paths_provider is None:
            return []
        return list(self.pending_test_paths_provider())
