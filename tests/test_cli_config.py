from pico.cli import build_agent, build_arg_parser, main
from pico.config import PicoConfigError
from pico.providers.clients import OpenAICompatibleModelClient

PROVIDER_ENV_NAMES = (
    "PICO_PROVIDER",
    "PICO_OPENAI_MODEL",
    "PICO_OPENAI_API_BASE",
    "PICO_OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "PICO_DEEPSEEK_MODEL",
    "PICO_DEEPSEEK_API_BASE",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_API_BASE",
    "DEEPSEEK_API_KEY",
)


def clear_provider_environment(monkeypatch):
    for name in PROVIDER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_launcher_env_has_priority_over_target_workspace_env(tmp_path, monkeypatch):
    clear_provider_environment(monkeypatch)
    launcher = tmp_path / "pico-launcher"
    workspace = tmp_path / "target-workspace"
    launcher.mkdir()
    workspace.mkdir()
    state_root = tmp_path / "state"
    launcher.joinpath(".env").write_text(
        "\n".join(
            (
                "PICO_PROVIDER=openai",
                "PICO_OPENAI_MODEL=qwen3.8-flash",
                "PICO_OPENAI_API_BASE=https://launcher.invalid/v1",
                "PICO_OPENAI_API_KEY=launcher-key",
                f"PICO_STATE_ROOT={state_root.as_posix()}",
            )
        ),
        encoding="utf-8",
    )
    workspace.joinpath(".env").write_text(
        "PICO_PROVIDER=deepseek\n"
        "PICO_DEEPSEEK_MODEL=wrong-model\n"
        "PICO_DEEPSEEK_API_BASE=https://workspace.invalid/v1\n"
        "PICO_DEEPSEEK_API_KEY=wrong-key",
        encoding="utf-8",
    )
    monkeypatch.chdir(launcher)
    args = build_arg_parser().parse_args(["--cwd", str(workspace)])

    agent = build_agent(args)

    assert isinstance(agent.model_client, OpenAICompatibleModelClient)
    assert agent.model_client.model == "qwen3.8-flash"
    assert agent.model_client.base_url == "https://launcher.invalid/v1"
    assert agent.model_client.api_key == "launcher-key"


def test_missing_provider_never_falls_back_to_a_cloud_backend(tmp_path, monkeypatch):
    clear_provider_environment(monkeypatch)
    launcher = tmp_path / "launcher"
    workspace = tmp_path / "workspace"
    launcher.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(launcher)
    monkeypatch.setenv("PICO_STATE_ROOT", str(tmp_path / "state"))
    args = build_arg_parser().parse_args(["--cwd", str(workspace)])

    try:
        build_agent(args)
    except PicoConfigError as exc:
        assert "PICO_PROVIDER is not configured" in str(exc)
        assert "silently select" in str(exc)
    else:
        raise AssertionError("missing provider unexpectedly selected a backend")


def test_main_reports_missing_provider_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    clear_provider_environment(monkeypatch)
    launcher = tmp_path / "launcher"
    workspace = tmp_path / "workspace"
    launcher.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(launcher)
    monkeypatch.setenv("PICO_STATE_ROOT", str(tmp_path / "state"))

    exit_code = main(["--cwd", str(workspace)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "PICO_PROVIDER is not configured" in captured.err
    assert "Traceback" not in captured.err
