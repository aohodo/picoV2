"""Project-local configuration helpers."""

import os
import re
from pathlib import Path

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PicoConfigError(ValueError):
    """A startup configuration error detected before provider I/O."""


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        raise PicoConfigError(f"invalid .env line: {line}")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise PicoConfigError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for path in (current, *current.parents):
        env_path = path / ".env"
        if env_path.exists():
            return env_path
    return None


def load_project_env(start, override=True):
    env_path = find_project_env(start)
    if env_path is None:
        return {}
    loaded = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PicoConfigError(f"could not load {env_path}: {exc}") from exc
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def load_runtime_env(launch_root, workspace_root):
    """Load workspace values, then give Pico's launcher .env final authority."""
    launch_path = find_project_env(launch_root)
    workspace_path = find_project_env(workspace_root)
    loaded = {}
    if workspace_path is not None and workspace_path != launch_path:
        loaded.update(load_project_env(workspace_path, override=True))
    if launch_path is not None:
        loaded.update(load_project_env(launch_path, override=True))
    return loaded


def provider_env(name, legacy_names=(), default=""):
    for env_name in (name, *legacy_names):
        value = os.environ.get(env_name)
        if value:
            return value
    return default


def require_provider_value(value, setting, provider):
    value = str(value or "").strip()
    if value:
        return value
    raise PicoConfigError(
        f"{provider} provider requires {setting}; define it in .env or pass the "
        "corresponding explicit CLI option"
    )
