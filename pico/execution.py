"""Command execution inside a transactional workspace.

Isolation belongs to the Pico deployment boundary. This module binds a
process to the transaction's shadow working directory and supplies a scrubbed
environment; it does not claim to be a host security sandbox.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


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
        try:
            result = subprocess.run(
                [*prefix, command],
                cwd=self.execution_root,
                env=env,
                capture_output=True,
                timeout=int(timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"shell command timed out after {timeout}s") from exc
        return {
            "exit_code": result.returncode,
            "stdout": self.secret_boundary.sanitize_text(result.stdout.decode("utf-8", errors="replace")),
            "stderr": self.secret_boundary.sanitize_text(result.stderr.decode("utf-8", errors="replace")),
            "shell_profile": self.profile_view(),
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
