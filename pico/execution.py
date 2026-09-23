"""Command execution inside a transactional workspace.

Isolation belongs to the Pico deployment boundary. This module binds a
process to the transaction's shadow working directory and supplies a scrubbed
environment; it does not claim to be a host security sandbox.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

MAX_CAPTURE_BYTES_PER_STREAM = 256 * 1024


class _BoundedCapture:
    def __init__(self, limit=MAX_CAPTURE_BYTES_PER_STREAM):
        self.limit = int(limit)
        self.head_limit = (self.limit * 3) // 4
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk):
        chunk = bytes(chunk)
        self.total += len(chunk)
        missing_head = max(0, self.head_limit - len(self.head))
        if missing_head:
            self.head.extend(chunk[:missing_head])
            chunk = chunk[missing_head:]
        if chunk:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    @property
    def truncated(self):
        return self.total > self.limit

    def value(self):
        if not self.truncated:
            return bytes(self.head + self.tail)
        omitted = self.total - len(self.head) - len(self.tail)
        marker = f"\n...[stream capture omitted {omitted} bytes]...\n".encode()
        return bytes(self.head) + marker + bytes(self.tail)


def _drain_stream(stream, capture):
    try:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            capture.add(chunk)
    finally:
        stream.close()


class ExecutionRuntimeUnavailable(RuntimeError):
    code = "shell_runtime_unavailable"


@dataclass(frozen=True)
class ExecutionProfile:
    argv_prefix: tuple
    dialect: str
    executable: str

    def view(self):
        return {"dialect": self.dialect, "executable": self.executable}


class WorkspaceCommandRunner:
    """Run commands in one TSW shadow without starting nested containers."""

    backend = "workspace-process"
    isolation = "deployment-boundary"

    def __init__(self, execution_root, secret_boundary, env_allowlist=()):
        self.execution_root = Path(execution_root).resolve()
        self.secret_boundary = secret_boundary
        self.env_allowlist = tuple(env_allowlist)
        self.started = False
        self._profile = None
        self._profile_error = ""
        try:
            prefix, dialect = self.shell()
            self._profile = ExecutionProfile(tuple(prefix), dialect, str(prefix[0]))
        except ExecutionRuntimeUnavailable as exc:
            self._profile_error = str(exc)

    @staticmethod
    def _windows_shell():
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            candidates = (
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            )
            for candidate in candidates:
                if candidate.is_file():
                    return [str(candidate), "--noprofile", "--norc", "-c"], "bash"
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            return [powershell, "-NoProfile", "-NonInteractive", "-Command"], "powershell"
        raise ExecutionRuntimeUnavailable("shell_runtime_unavailable: no supported command shell was found")

    @classmethod
    def shell(cls):
        if os.name == "nt":
            return cls._windows_shell()
        bash = shutil.which("bash")
        if bash:
            return [bash, "--noprofile", "--norc", "-c"], "bash"
        shell = shutil.which("sh")
        if shell:
            return [shell, "-c"], "sh"
        raise ExecutionRuntimeUnavailable("shell_runtime_unavailable: no supported command shell was found")

    def start(self):
        if not self.execution_root.is_dir():
            raise ExecutionRuntimeUnavailable(
                f"shell_runtime_unavailable: transaction workspace is missing: {self.execution_root}"
            )
        self.started = True
        return self

    def profile_view(self):
        if self._profile is None:
            return {"dialect": "unavailable", "executable": "", "error": self._profile_error}
        return self._profile.view()

    @staticmethod
    def _prepend_runtime_path(env):
        runtime_dir = str(Path(sys.executable).resolve().parent)
        current = str(env.get("PATH", ""))
        entries = [item for item in current.split(os.pathsep) if item]
        if runtime_dir not in entries:
            entries.insert(0, runtime_dir)
        env["PATH"] = os.pathsep.join(entries)
        return env

    @staticmethod
    def _run_process(argv, cwd, env, timeout):
        process_options = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **process_options,
        )
        stdout_capture = _BoundedCapture()
        stderr_capture = _BoundedCapture()
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_capture),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_capture),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            exit_code = process.wait(timeout=int(timeout))
        except subprocess.TimeoutExpired as exc:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            raise TimeoutError(f"command timed out after {timeout}s") from exc
        stdout_thread.join()
        stderr_thread.join()
        return {
            "exit_code": exit_code,
            "stdout": stdout_capture.value(),
            "stderr": stderr_capture.value(),
            "stdout_bytes": stdout_capture.total,
            "stderr_bytes": stderr_capture.total,
            "stdout_truncated": stdout_capture.truncated,
            "stderr_truncated": stderr_capture.truncated,
        }

    def run(self, command, timeout=20):
        self.start()
        if self._profile is None:
            raise ExecutionRuntimeUnavailable(self._profile_error or "shell_runtime_unavailable")
        prefix = list(self._profile.argv_prefix)
        shell_kind = self._profile.dialect
        command = str(command)
        if shell_kind == "bash":
            host_python = str(Path(sys.executable).resolve())
            command = command.replace(host_python, host_python.replace("\\", "/"))
            command = "set -o pipefail\n" + command
        env = self.secret_boundary.build_sandbox_env(
            self.env_allowlist,
            extra={
                "PWD": str(self.execution_root),
                "PICO_AGENT": "1",
                "PICO_SHELL_DIALECT": shell_kind,
            },
        )
        env = self._prepend_runtime_path(env)
        result = self._run_process(
            [*prefix, command], self.execution_root, env, timeout
        )
        return {
            **result,
            "stdout": self.secret_boundary.sanitize_text(
                result["stdout"].decode("utf-8", errors="replace")
            ),
            "stderr": self.secret_boundary.sanitize_text(
                result["stderr"].decode("utf-8", errors="replace")
            ),
            "shell_profile": self.profile_view(),
        }

    def run_argv(self, argv, timeout=20):
        """Run one executable directly so its exit status cannot be shell-masked."""
        self.start()
        argv = [str(item) for item in argv]
        if not argv or not argv[0].strip():
            raise ValueError("argv must contain an executable")
        if argv[0].lower() in {"python", "python.exe", "python3", "python3.exe"}:
            argv[0] = str(Path(sys.executable).resolve())
        # An unqualified shell name refers to this workspace's selected shell,
        # not a different installation (e.g. the Windows WSL launcher).
        # Explicit executable paths keep their caller-requested meaning.
        if (
            self._profile is not None
            and argv[0].casefold() in {self._profile.dialect, self._profile.dialect + ".exe"}
        ):
            argv[0] = self._profile.executable
        env = self.secret_boundary.build_sandbox_env(
            self.env_allowlist,
            extra={
                "PWD": str(self.execution_root),
                "PICO_AGENT": "1",
                "PICO_SHELL_DIALECT": "direct",
            },
        )
        env = self._prepend_runtime_path(env)
        profile = {"dialect": "direct", "executable": argv[0]}
        process_argv = argv
        resolved_executable = shutil.which(argv[0], path=env.get("PATH"))
        if os.name == "nt" and resolved_executable and Path(resolved_executable).suffix.casefold() in {".cmd", ".bat"}:
            if any(re.search(r"[&|<>^\r\n]", item) for item in argv):
                raise ValueError("batch verification argv contains shell metacharacters")
            command_processor = env.get("COMSPEC") or shutil.which("cmd.exe")
            if not command_processor:
                raise ExecutionRuntimeUnavailable("verification_runtime_unavailable: cmd.exe was not found")
            process_argv = [command_processor, "/d", "/s", "/c", subprocess.list2cmdline([resolved_executable, *argv[1:]])]
            profile = {"dialect": "direct-batch", "executable": resolved_executable}
        try:
            result = self._run_process(
                process_argv, self.execution_root, env, timeout
            )
        except OSError as exc:
            raise ExecutionRuntimeUnavailable(
                f"verification_runtime_unavailable: {exc}"
            ) from exc
        return {
            **result,
            "stdout": self.secret_boundary.sanitize_text(
                result["stdout"].decode("utf-8", errors="replace")
            ),
            "stderr": self.secret_boundary.sanitize_text(
                result["stderr"].decode("utf-8", errors="replace")
            ),
            "shell_profile": profile,
        }

    def stop(self):
        self.started = False


class ExecutionLease:
    def __init__(self, runner, owns_runner=True):
        self.runner = runner
        self.owns_runner = bool(owns_runner)

    def borrow(self):
        return ExecutionLease(self.runner, owns_runner=False)

    def stop(self):
        if self.owns_runner:
            self.runner.stop()


def format_shell_result(result):
    profile = result.get("shell_profile") or {}
    return (
        f"exit_code: {result['exit_code']}\n"
        f"shell: {profile.get('dialect', 'unknown')}\n"
        f"stdout:\n{result['stdout'].strip() or '(empty)'}\n"
        f"stderr:\n{result['stderr'].strip() or '(empty)'}"
    )
