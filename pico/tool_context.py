"""Narrow context passed from runtime into tool functions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    command_runner: object = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()
