"""Shadow workspace and guarded, journaled source commit."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .path_support import native_path
from .workspace import IGNORED_PATH_NAMES, remove_workspace_tree

TRANSACTION_SCHEMA_VERSION = "tsw-v1"
TERMINAL_STATES = {"COMMITTED", "DISCARDED"}
COPY_EXCLUDES = set(IGNORED_PATH_NAMES) | {
    ".pico", ".venv", "venv", "node_modules", "target", "build", "dist",
    ".ssh", ".aws", ".azure", ".docker", ".gnupg", ".netrc", "_netrc", ".npmrc", ".pypirc",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path):
    digest = hashlib.sha256()
    with native_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value):
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe transaction path: {value}")
    normalized = path.as_posix().lstrip("./")
    if not normalized:
        raise ValueError("empty transaction path")
    return normalized


def _manifest(root, excludes=(), includes=None):
    root = native_path(root)
    excluded = set(excludes)
    result = {}
    if not root.exists():
        return result
    if includes is None:
        paths = root.rglob("*")
    else:
        paths = (root / relative for relative in sorted(includes))
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in excluded for part in relative.parts):
            continue
        key = relative.as_posix()
        if path.is_symlink():
            result[key] = {"type": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            stat = path.stat()
            result[key] = {
                "type": "file",
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "hash": _hash_file(path),
            }
    return result


def _git_view_paths(root):
    """Return the Git working view: tracked plus non-ignored untracked files."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.longpaths=true", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git workspace enumeration failed: {exc}") from exc
    return {
        Path(item).as_posix()
        for item in result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if item
    }


def _is_git_workspace(root):
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    # A plain directory nested under somebody else's repository is not itself
    # a Git workspace. Treating it as one makes `git ls-files` return paths
    # relative to the parent repository and corrupts the shadow view.
    return Path(result.stdout.strip()).resolve() == Path(root).resolve()


def _git_user_owned(root):
    try:
        result = subprocess.run(
            ["git", "-c", "core.longpaths=true", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root, capture_output=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    owned = set()
    for record in result.stdout.decode("utf-8", errors="replace").split("\0"):
        if not record:
            continue
        path = record[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            owned.add(Path(path).as_posix())
    return owned


def _copy_view(source, destination, includes=None):
    source = native_path(source)
    destination = native_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if includes is not None:
        for relative in sorted(includes):
            entry = source / relative
            if not entry.exists() and not entry.is_symlink():
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.is_symlink():
                try:
                    target.symlink_to(os.readlink(entry), target_is_directory=entry.is_dir())
                except OSError:
                    continue
            elif entry.is_file():
                shutil.copy2(entry, target)
        return
    for entry in source.iterdir():
        if entry.name in COPY_EXCLUDES or entry.name == ".git":
            continue
        target = destination / entry.name
        if entry.is_symlink():
            try:
                target.symlink_to(os.readlink(entry), target_is_directory=entry.is_dir())
            except OSError:
                continue
        elif entry.is_dir():
            shutil.copytree(
                entry, target, symlinks=True,
                ignore=shutil.ignore_patterns(*COPY_EXCLUDES), dirs_exist_ok=True,
            )
        else:
            shutil.copy2(entry, target)


class WorkspaceBackend:
    name = "copy"

    def create(self, source_root, execution_root, includes=None):
        _copy_view(source_root, execution_root, includes=includes)


class CopyWorkspaceBackend(WorkspaceBackend):
    name = "copy"


class GitShadowBackend(WorkspaceBackend):
    name = "git-shadow"

    def create(self, source_root, execution_root, includes=None):
        _copy_view(source_root, execution_root, includes=includes)


class TransactionalWorkspace:
    def __init__(
        self, source_root, transaction_root, secret_boundary=None, transaction_id=None,
        storage_limit_bytes=10 * 1024 ** 3,
    ):
        self.source_root = Path(source_root).resolve()
        self.transaction_id = transaction_id or "txn_" + uuid.uuid4().hex[:12]
        self.transaction_root = Path(transaction_root).resolve() / self.transaction_id
        self.execution_root = self.transaction_root / "execution"
        self.recovery_root = self.transaction_root / "recovery"
        self.secret_boundary = secret_boundary
        self.storage_limit_bytes = int(storage_limit_bytes)
        self.transaction_path = self.transaction_root / "transaction.json"
        self.baseline_path = self.transaction_root / "baseline.json"
        self.staged_path = self.transaction_root / "staged-manifest.json"
        self.journal_path = self.transaction_root / "commit-journal.json"
        self.state = "NEW"
        self.backend_name = ""
        self.baseline = {}
        self.execution_baseline = {}
        self.user_owned_paths = set()
        self.protected_paths = set()

    @classmethod
    def load(cls, source_root, transaction_root, transaction_id, secret_boundary=None):
        instance = cls(source_root, transaction_root, secret_boundary, transaction_id)
        data = json.loads(instance.transaction_path.read_text(encoding="utf-8"))
        instance.state = data["state"]
        instance.backend_name = data.get("backend", "copy")
        instance.user_owned_paths = set(data.get("user_owned_paths", []))
        instance.protected_paths = set(data.get("protected_paths", []))
        baseline_data = json.loads(instance.baseline_path.read_text(encoding="utf-8"))
        if "source" in baseline_data:
            instance.baseline = baseline_data["source"]
            instance.execution_baseline = baseline_data.get("execution", baseline_data["source"])
        else:
            instance.baseline = baseline_data
            instance.execution_baseline = baseline_data
        if instance.state in {"ACTIVE", "COMMITTING"}:
            instance.state = "RECOVERY_REQUIRED" if instance.recovery_required() else "INTERRUPTED"
            instance._persist(recovered_from=data["state"])
        return instance

    def _write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.secret_boundary:
            payload = self.secret_boundary.sanitize_object(payload)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp = Path(handle.name)
        temp.replace(path)

    def _persist(self, **extra):
        payload = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "state": self.state,
            "source_root": str(self.source_root),
            "execution_root": str(self.execution_root),
            "backend": self.backend_name,
            "user_owned_paths": sorted(self.user_owned_paths),
            "protected_paths": sorted(self.protected_paths),
            "updated_at": _now(),
            **extra,
        }
        self._write_json(self.transaction_path, payload)

    def begin(self):
        if self.state != "NEW":
            raise RuntimeError(f"transaction cannot begin from {self.state}")
        self.transaction_root.mkdir(parents=True, exist_ok=False)
        try:
            is_git = _is_git_workspace(self.source_root)
            view_paths = _git_view_paths(self.source_root) if is_git else None
            self.baseline = _manifest(
                self.source_root, COPY_EXCLUDES | {".git"}, includes=view_paths
            )
            self.user_owned_paths = _git_user_owned(self.source_root) if is_git else set()
            backend = GitShadowBackend() if is_git else CopyWorkspaceBackend()
            backend.create(self.source_root, self.execution_root, includes=view_paths)
            self.backend_name = backend.name
            self._scrub_registered_secrets()
            if is_git:
                self._initialize_shadow_git()
            self.execution_baseline = _manifest(self.execution_root, COPY_EXCLUDES | {".git"})
            self._enforce_storage_limit()
            self._write_json(
                self.baseline_path,
                {"source": self.baseline, "execution": self.execution_baseline},
            )
            self.state = "ACTIVE"
            self._persist(created_at=_now())
        except Exception as exc:
            # A transaction is not observable until ACTIVE. Failed materialization
            # must leave neither a resumable record nor a partial shadow tree.
            shutil.rmtree(self.transaction_root, ignore_errors=True)
            raise RuntimeError(f"transaction workspace creation failed: {exc}") from exc
        return self

    def _initialize_shadow_git(self):
        commands = (
            ["git", "init"],
            ["git", "config", "core.longpaths", "true"],
            ["git", "config", "user.email", "pico@localhost.invalid"],
            ["git", "config", "user.name", "Pico Shadow"],
            ["git", "add", "--all"],
            ["git", "commit", "-m", "Pico transaction baseline"],
        )
        for command in commands:
            result = subprocess.run(
                command,
                cwd=self.execution_root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git shadow initialization failed: {result.stderr.strip()}")

    def _scrub_registered_secrets(self):
        if not self.secret_boundary or not self.secret_boundary.registered_values:
            return
        execution_root = native_path(self.execution_root)
        for path in execution_root.rglob("*"):
            if not path.is_file() or path.is_symlink() or ".git" in path.parts:
                continue
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            sanitized = self.secret_boundary.sanitize_text(text)
            if sanitized != text:
                path.write_text(sanitized, encoding="utf-8")
                self.protected_paths.add(path.relative_to(execution_root).as_posix())

    def _enforce_storage_limit(self):
        size = 0
        for path in native_path(self.execution_root).rglob("*"):
            if path.is_file() and not path.is_symlink():
                size += path.stat().st_size
                if size > self.storage_limit_bytes:
                    raise RuntimeError("workspace_storage_limit")

    def enforce_storage_limit(self):
        self._enforce_storage_limit()

    def diff(self):
        staged = _manifest(self.execution_root, COPY_EXCLUDES | {".git"})
        changes = []
        for relative in sorted(set(self.execution_baseline) | set(staged)):
            before = self.execution_baseline.get(relative)
            after = staged.get(relative)
            if before == after:
                continue
            if before is None:
                operation = "create"
            elif after is None:
                operation = "delete"
            else:
                operation = "modify"
            changes.append({"path": relative, "operation": operation, "before": before, "after": after})
        return changes

    def stage(self):
        if self.state not in {"ACTIVE", "INTERRUPTED"}:
            raise RuntimeError(f"transaction cannot stage from {self.state}")
        changes = self.diff()
        self._write_json(self.staged_path, {"changes": changes, "created_at": _now()})
        self.state = "STAGED"
        self._persist(change_count=len(changes))
        return changes

    def interrupt(self, reason="interrupted"):
        if self.state not in TERMINAL_STATES:
            self.state = "INTERRUPTED"
            self._persist(interrupt_reason=reason)

    def validate_commit(self):
        if self.state not in {"STAGED", "VALIDATING", "READY_FOR_REVIEW"}:
            raise RuntimeError(f"transaction cannot validate from {self.state}")
        self.state = "VALIDATING"
        self._persist()
        changes = self.diff()
        conflicts = []
        current_paths = _git_view_paths(self.source_root) if self.backend_name == "git-shadow" else None
        current = _manifest(
            self.source_root,
            COPY_EXCLUDES | {".git"},
            includes=current_paths,
        )
        for change in changes:
            path = change["path"]
            if path in self.protected_paths:
                conflicts.append({"path": path, "reason": "protected_secret_path"})
            if path in self.user_owned_paths:
                conflicts.append({"path": path, "reason": "user_owned_path"})
            if current.get(path) != self.baseline.get(path):
                conflicts.append({"path": path, "reason": "source_changed"})
            try:
                (self.source_root / path).parent.resolve().relative_to(self.source_root)
            except ValueError:
                conflicts.append({"path": path, "reason": "unsafe_source_path"})
        if conflicts:
            self.state = "CONFLICTED"
            self._persist(conflicts=conflicts)
            return conflicts
        self.state = "READY_FOR_REVIEW"
        self._persist(change_count=len(changes))
        return []

    def block_validation(self, reason):
        if self.state not in {"STAGED", "VALIDATING", "READY_FOR_REVIEW"}:
            raise RuntimeError(f"transaction cannot block validation from {self.state}")
        conflicts = [{"path": "", "reason": str(reason)}]
        self.state = "CONFLICTED"
        self._persist(conflicts=conflicts)
        return conflicts

    def commit(self):
        if self.state != "READY_FOR_REVIEW":
            raise RuntimeError(f"transaction cannot commit from {self.state}")
        if self.validate_commit():
            raise RuntimeError("workspace_conflict")
        changes = self.diff()
        self.state = "COMMITTING"
        journal = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "state": self.state,
            "operations": changes,
            "completed_operations": [],
            "created_at": _now(),
        }
        self._write_json(self.journal_path, journal)
        native_path(self.recovery_root).mkdir(parents=True, exist_ok=True)
        try:
            for change in changes:
                self._apply_change(change)
                journal["completed_operations"].append(change["path"])
                self._write_json(self.journal_path, journal)
        except Exception as exc:
            journal["state"] = "COMMIT_FAILED"
            journal["error"] = str(exc)
            self._write_json(self.journal_path, journal)
            self.state = "COMMIT_FAILED"
            self._rollback_completed(changes, journal["completed_operations"])
            self._persist(error=str(exc))
            raise
        journal["state"] = "COMMITTED"
        self._write_json(self.journal_path, journal)
        self.state = "COMMITTED"
        self._persist(committed_at=_now(), change_count=len(changes))
        shutil.rmtree(self.recovery_root, ignore_errors=True)
        try:
            remove_workspace_tree(self.execution_root)
        except OSError as exc:
            self._persist(
                committed_at=_now(),
                change_count=len(changes),
                shadow_cleanup_pending=True,
                shadow_cleanup_error=str(exc),
            )
        return changes

    def _apply_change(self, change):
        relative = _safe_relative(change["path"])
        source = native_path(self.source_root / relative)
        staged = native_path(self.execution_root / relative)
        recovery = native_path(self.recovery_root / relative)
        if source.exists() or source.is_symlink():
            recovery.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                recovery.write_text(json.dumps({"symlink": os.readlink(source)}), encoding="utf-8")
            else:
                shutil.copy2(source, recovery)
        if change["operation"] == "delete":
            source.unlink()
            return
        source.parent.mkdir(parents=True, exist_ok=True)
        if staged.is_symlink():
            temp = source.with_name(source.name + ".pico-tmp-" + uuid.uuid4().hex[:6])
            temp.symlink_to(os.readlink(staged), target_is_directory=staged.is_dir())
            temp.replace(source)
            return
        with tempfile.NamedTemporaryFile(delete=False, dir=source.parent, prefix=source.name + ".pico-") as handle:
            temp = Path(handle.name)
            with staged.open("rb") as staged_handle:
                shutil.copyfileobj(staged_handle, handle)
        shutil.copystat(staged, temp)
        temp.replace(source)

    def _rollback_completed(self, changes, completed):
        for change in reversed(changes):
            if change["path"] not in completed:
                continue
            target = native_path(self.source_root / change["path"])
            recovery = native_path(self.recovery_root / change["path"])
            try:
                if change["before"] is None:
                    if target.exists() or target.is_symlink():
                        target.unlink()
                elif recovery.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    recovery.replace(target)
            except OSError:
                self.state = "RECOVERY_REQUIRED"

    def discard(self):
        if self.state in {"COMMITTING", "COMMITTED"}:
            raise RuntimeError(f"transaction cannot discard from {self.state}")
        self.state = "DISCARDED"
        self._persist(discarded_at=_now())
        remove_workspace_tree(self.execution_root)

    def recovery_required(self):
        if not self.journal_path.exists():
            return False
        data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        return data.get("state") in {"COMMITTING", "COMMIT_FAILED", "RECOVERY_REQUIRED"}
