import subprocess
from pathlib import Path

from pico.execution import WorkspaceCommandRunner
from pico.progress.verification_evidence import VerificationProbe
from pico.security import SecretBoundary
from pico.workspace import WorkspaceContext
from pico.workspace.transactional_workspace import TransactionalWorkspace


def _capture_runner(tmp_path):
    runner = WorkspaceCommandRunner(tmp_path, SecretBoundary(env={"PATH": ""}))
    captured = []

    def capture(argv, cwd, env, timeout):
        captured.append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        return {"exit_code": 0, "stdout": b"", "stderr": b""}

    runner._run_process = capture
    return runner, captured


def _assert_transaction_local_environment(env, workspace):
    runtime_root = (workspace / ".pico" / "runtime").resolve()
    assert Path(env["PICO_RUNTIME_ROOT"]).resolve() == runtime_root
    for name in (
        "TMP",
        "TEMP",
        "TMPDIR",
        "PYTHONUSERBASE",
        "PYTHONPATH",
        "PIP_TARGET",
        "PIP_CACHE_DIR",
        "UV_CACHE_DIR",
        "UV_TOOL_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "NPM_CONFIG_PREFIX",
        "NPM_CONFIG_CACHE",
        "GRADLE_USER_HOME",
    ):
        assert runtime_root in Path(env[name]).resolve().parents
    assert str(runtime_root / "maven") in env["MAVEN_OPTS"]
    assert env["PYTHONNOUSERSITE"] == "1"


def test_shell_and_direct_commands_share_transaction_local_tool_state(tmp_path):
    runner, captured = _capture_runner(tmp_path)

    runner.run("python -V")
    runner.run_argv(["python", "-V"])

    assert len(captured) == 2
    _assert_transaction_local_environment(captured[0]["env"], tmp_path)
    _assert_transaction_local_environment(captured[1]["env"], tmp_path)
    assert captured[0]["env"]["PICO_SHELL_DIALECT"] == runner.profile_view()["dialect"]
    assert captured[1]["env"]["PICO_SHELL_DIALECT"] == "direct"


def test_maven_cache_is_shared_runtime_state_not_transaction_source(tmp_path):
    shadow = tmp_path / "shadow"
    cache = tmp_path / "state" / "cache"
    shadow.mkdir()
    runner = WorkspaceCommandRunner(
        shadow,
        SecretBoundary(env={"PATH": ""}),
        cache_root=cache,
    )
    captured = []
    runner._run_process = lambda argv, cwd, env, timeout: (
        captured.append(env)
        or {"exit_code": 0, "stdout": b"", "stderr": b""}
    )

    runner.run_argv(["mvn", "-q", "test"])

    assert str((cache / "maven").resolve()) in captured[0]["MAVEN_OPTS"]
    assert not (shadow / ".pico" / "runtime" / "maven").exists()


def test_transaction_runtime_state_never_enters_source_diff(tmp_path):
    source = tmp_path / "source"
    transactions = tmp_path / "transactions"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    transaction = TransactionalWorkspace(source, transactions).begin()
    runtime_file = transaction.execution_root / ".pico" / "runtime" / "python-packages" / "demo.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("HOST_SIDE_EFFECT = False\n", encoding="utf-8")

    assert transaction.diff() == []


def test_git_workspace_discovery_decodes_unicode_path_as_utf8(tmp_path):
    source = tmp_path / "代码仓库🙂"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    (source / "说明.txt").write_text("内容\n", encoding="utf-8")

    workspace = WorkspaceContext.build(source)
    transaction = TransactionalWorkspace(source, tmp_path / "transactions").begin()

    assert Path(workspace.repo_root) == source.resolve()
    assert transaction.backend_name == "git-shadow"


def test_java_report_evidence_distinguishes_executed_from_ignored_test(tmp_path):
    changed = "src/test/java/demo/UserServiceTestAI.java"
    probe = VerificationProbe(tmp_path, ["mvn", "test"], [changed])
    report = tmp_path / "target" / "surefire-reports" / "TEST-demo.UserServiceTest.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        '<testsuite tests="1"><testcase classname="demo.UserServiceTest" name="works"/></testsuite>',
        encoding="utf-8",
    )

    evidence = probe.finish()

    assert evidence["test_count"] == 1
    assert evidence["verified_test_paths"] == []
    assert evidence["missing_test_paths"] == [changed]


def test_windows_batch_preserves_executable_and_argument_boundaries():
    argv = WorkspaceCommandRunner._windows_batch_argv(
        "C:/Windows/System32/cmd.exe",
        "C:/Program Files/nodejs/npm.CMD",
        ["run", "build:prod", "--", "value with spaces"],
    )

    assert argv == [
        "C:/Windows/System32/cmd.exe",
        "/d",
        "/s",
        "/c",
        "call",
        "C:/Program Files/nodejs/npm.CMD",
        "run",
        "build:prod",
        "--",
        "value with spaces",
    ]


def test_relative_resolved_batch_launcher_is_anchored_to_workspace(tmp_path):
    resolved = WorkspaceCommandRunner._absolute_executable_path(
        ".\\mvn.CMD", tmp_path
    )

    assert resolved == str((tmp_path / "mvn.CMD").resolve())


def test_git_bash_adapts_cmd_null_redirection_on_windows(monkeypatch):
    monkeypatch.setattr("pico.execution.command_runner.os.name", "nt")

    command = WorkspaceCommandRunner._normalize_command_for_shell(
        "where mvn 2>nul & echo ok > NUL", "bash"
    )

    assert command == "where mvn 2>/dev/null & echo ok > /dev/null"


def test_null_redirection_is_not_rewritten_for_powershell(monkeypatch):
    monkeypatch.setattr("pico.execution.command_runner.os.name", "nt")

    command = WorkspaceCommandRunner._normalize_command_for_shell(
        "where mvn 2>nul", "powershell"
    )

    assert command == "where mvn 2>nul"


def test_node_dependencies_are_copied_privately_on_first_node_command(tmp_path):
    source = tmp_path / "source"
    shadow = tmp_path / "shadow"
    source_dependency = source / "node_modules" / "demo" / "index.js"
    source_dependency.parent.mkdir(parents=True)
    source_dependency.write_text("module.exports = 1\n", encoding="utf-8")
    shadow.mkdir()
    runner = WorkspaceCommandRunner(
        shadow, SecretBoundary(env={"PATH": ""}), source_root=source
    )
    captured = []
    runner._run_process = lambda argv, cwd, env, timeout: (
        captured.append(argv)
        or {"exit_code": 0, "stdout": b"", "stderr": b""}
    )

    runner.run_argv(["node", "--version"])

    copied = shadow / "node_modules" / "demo" / "index.js"
    assert copied.read_text(encoding="utf-8") == "module.exports = 1\n"
    copied.write_text("module.exports = 2\n", encoding="utf-8")
    assert source_dependency.read_text(encoding="utf-8") == "module.exports = 1\n"
    assert captured
