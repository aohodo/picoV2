"""Session JSON persistence."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from .file_lock import FileLock

SESSION_SCHEMA_VERSION = 1


class SessionError(RuntimeError):
    """Base class for controlled session persistence failures."""


class SessionLoadError(SessionError):
    def __init__(self, path, detail):
        path = Path(path)
        backup = path.with_suffix(path.suffix + ".bak")
        recovery = f" Restore {backup} or start a new session." if backup.exists() else " Start a new session."
        super().__init__(f"session file is corrupt: {path} ({detail}).{recovery}")
        self.path = path


class SessionConflictError(SessionError):
    def __init__(self, session_id, expected_revision, current_revision):
        super().__init__(
            "session_revision_conflict: "
            f"session {session_id} expected revision {expected_revision}, current revision is {current_revision}"
        )
        self.session_id = str(session_id)
        self.expected_revision = int(expected_revision)
        self.current_revision = int(current_revision)


class SessionStore:
    def __init__(self, root, secret_boundary=None):
        self.root = Path(root)
        self.secret_boundary = secret_boundary
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def lock_path(self, session_id):
        return self.root / f"{session_id}.lock"

    @staticmethod
    def _decode(path):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionLoadError(path, str(exc)) from exc
        if not isinstance(payload, dict):
            raise SessionLoadError(path, "root value is not an object")
        try:
            revision = int(payload.get("revision", 0))
            schema_version = int(payload.get("schema_version", SESSION_SCHEMA_VERSION))
        except (TypeError, ValueError) as exc:
            raise SessionLoadError(path, "schema_version or revision is invalid") from exc
        if revision < 0:
            raise SessionLoadError(path, "revision is negative")
        if schema_version != SESSION_SCHEMA_VERSION:
            raise SessionLoadError(path, f"unsupported schema_version {schema_version}")
        payload["schema_version"] = schema_version
        payload["revision"] = revision
        return payload

    @staticmethod
    def _write_atomic(path, payload):
        path = Path(path)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=path.parent,
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def save(self, session):
        session_id = str(session["id"])
        path = self.path(session_id)
        expected_revision = int(session.get("revision", 0))
        with FileLock(self.lock_path(session_id)):
            current_revision = 0
            if path.exists():
                current_revision = int(self._decode(path)["revision"])
            if current_revision != expected_revision:
                raise SessionConflictError(session_id, expected_revision, current_revision)
            next_revision = current_revision + 1
            payload = dict(session)
            payload["schema_version"] = SESSION_SCHEMA_VERSION
            payload["revision"] = next_revision
            if self.secret_boundary:
                payload = self.secret_boundary.sanitize_object(payload)
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            self._write_atomic(path, payload)
            session["schema_version"] = SESSION_SCHEMA_VERSION
            session["revision"] = next_revision
        return path

    def load(self, session_id):
        path = self.path(session_id)
        with FileLock(self.lock_path(session_id)):
            return self._decode(path)

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
