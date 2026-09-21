"""Host-owned state locations for sessions, runs, and transactions."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def default_state_root(env=None):
    env = os.environ if env is None else env
    if os.name == "nt":
        base = env.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Pico"
        return Path(tempfile.gettempdir()) / "Pico"
    xdg = env.get("XDG_STATE_HOME")
    return Path(xdg) / "pico" if xdg else Path.home() / ".local" / "state" / "pico"


def _git_value(source_root, *args):
    try:
        result = subprocess.run(
            ["git", *args], cwd=source_root, capture_output=True, text=True, timeout=5, check=True
        )
        return result.stdout.strip()
    except Exception:
        return ""


def workspace_identity(source_root):
    source_root = Path(source_root).resolve()
    git_dir = _git_value(source_root, "rev-parse", "--absolute-git-dir")
    remote = _git_value(source_root, "config", "--get", "remote.origin.url")
    if git_dir:
        identity = f"git:{remote or Path(git_dir).resolve()}"
    else:
        identity = f"path:{source_root}"
    workspace_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return workspace_id, identity


class WorkspaceState:
    def __init__(self, source_root, root=None):
        self.source_root = Path(source_root).resolve()
        self.workspace_id, self.identity = workspace_identity(self.source_root)
        self.global_root = Path(root or default_state_root()).resolve()
        self.root = self.global_root / "workspaces" / self.workspace_id
        self.sessions = self.root / "sessions"
        self.runs = self.root / "runs"
        self.transactions = self.root / "transactions"
        self.memory = self.root / "memory"

    def ensure(self):
        for path in (self.sessions, self.runs, self.transactions, self.memory):
            path.mkdir(parents=True, exist_ok=True)
        metadata = {
            "workspace_id": self.workspace_id,
            "repository_identity": self.identity,
            "last_known_source_root": str(self.source_root),
        }
        (self.root / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self

    def cleanup_candidates(self, terminal_days=7, unfinished_days=30, now=None):
        now = now or datetime.now(timezone.utc)
        candidates = []
        for transaction_file in self.transactions.glob("*/transaction.json"):
            try:
                data = json.loads(transaction_file.read_text(encoding="utf-8"))
                updated = datetime.fromisoformat(data["updated_at"])
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            terminal = data.get("state") in {"COMMITTED", "DISCARDED"}
            ttl = timedelta(days=terminal_days if terminal else unfinished_days)
            if now - updated > ttl:
                candidates.append(transaction_file.parent)
        return candidates
