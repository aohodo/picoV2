"""Fail-closed execution plane for agent-controlled subprocesses."""

import shutil
import subprocess
import sys
import uuid
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    code = "shell_sandbox_unavailable"


class SandboxRunner:
    def start(self):
        raise NotImplementedError

    def run(self, command, timeout=20):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


class DockerSandboxRunner(SandboxRunner):
    def __init__(
        self, execution_root, secret_boundary, image="pico-sandbox:1",
        env_allowlist=("LANG", "LC_ALL", "LC_CTYPE", "TERM"),
        memory="2g", cpus="2", pids_limit=256,
    ):
        self.execution_root = Path(execution_root).resolve()
        self.secret_boundary = secret_boundary
        self.image = image
        self.env_allowlist = tuple(env_allowlist)
        self.memory = str(memory)
        self.cpus = str(cpus)
        self.pids_limit = int(pids_limit)
        self.container_name = "pico-" + uuid.uuid4().hex[:12]
        self.started = False

    @staticmethod
    def executable():
        discovered = shutil.which("docker")
        if discovered:
            return discovered
        windows_default = Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe")
        return str(windows_default) if windows_default.exists() else None

    @staticmethod
    def available():
        docker = DockerSandboxRunner.executable()
        if not docker:
            return False
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=10,
                check=False,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False

    def start(self):
        if self.started:
            return self
        if not self.available():
            raise SandboxUnavailable("shell_sandbox_unavailable: Docker is not available")
        command = [
            self.executable(), "run", "--detach", "--rm", "--name", self.container_name,
            "--workdir", "/workspace", "--mount", f"type=bind,source={self.execution_root},target=/workspace",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "--pids-limit", str(self.pids_limit), "--memory", self.memory, "--cpus", self.cpus,
            "--network", "bridge",
            "--mount", "source=pico-cache-pip,target=/home/pico/.cache/pip",
            "--mount", "source=pico-cache-uv,target=/home/pico/.cache/uv",
            "--mount", "source=pico-cache-maven,target=/home/pico/.m2/repository",
        ]
        for name, value in self.secret_boundary.build_sandbox_env(self.env_allowlist).items():
            command.extend(["--env", f"{name}={value}"])
        command.extend([self.image, "sleep", "infinity"])
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            raise SandboxUnavailable(
                "shell_sandbox_unavailable: " + self.secret_boundary.sanitize_text(result.stderr.strip())
            )
        self.started = True
        return self

    def run(self, command, timeout=20):
        self.start()
        command = str(command)
        for host_python in {sys.executable, str(Path(sys.executable).resolve())}:
            command = command.replace(f'"{host_python}"', "python3").replace(host_python, "python3")
        try:
            result = subprocess.run(
                [self.executable(), "exec", "-i", self.container_name, "bash", "-s"],
                input=("set -o pipefail\n" + str(command) + "\n").encode("utf-8"),
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
        }

    def stop(self):
        if self.started and self.executable():
            subprocess.run(
                [self.executable(), "rm", "-f", self.container_name],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        self.started = False


class SandboxLease:
    def __init__(self, runner, owns_runner=True):
        self.runner = runner
        self.owns_runner = bool(owns_runner)

    def borrow(self):
        return SandboxLease(self.runner, owns_runner=False)

    def stop(self):
        if self.owns_runner:
            self.runner.stop()


def format_shell_result(result):
    return (
        f"exit_code: {result['exit_code']}\n"
        f"stdout:\n{result['stdout'].strip() or '(empty)'}\n"
        f"stderr:\n{result['stderr'].strip() or '(empty)'}"
    )
