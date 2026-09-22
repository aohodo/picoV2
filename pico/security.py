"""Security and redaction helpers for runtime artifacts."""

import os
import re

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL),
)


class SecretBoundary:
    """One redaction boundary shared by runtime, stores, and sandbox setup.

    The hard guarantee applies to registered plaintext values. Pattern based
    detection is deliberately only defense in depth.
    """

    def __init__(self, secrets=(), secret_env_names=(), env=None):
        self.env = os.environ if env is None else env
        self.secret_env_names = _normalized_secret_names(secret_env_names)
        self._secrets = set()
        for _, value in detected_secret_env_items(self.env, self.secret_env_names):
            self.register_secret(value)
        for value in secrets:
            self.register_secret(value)

    @property
    def registered_values(self):
        return tuple(sorted(self._secrets, key=len, reverse=True))

    def register_secret(self, value):
        value = str(value or "")
        if value:
            self._secrets.add(value)
        return value

    def sanitize_text(self, text):
        sanitized = str(text)
        for value in self.registered_values:
            sanitized = sanitized.replace(value, REDACTED_VALUE)
        for pattern in SECRET_PATTERNS:
            sanitized = pattern.sub(REDACTED_VALUE, sanitized)
        return sanitized

    def sanitize_object(self, value, key=None):
        if key and is_secret_env_name(key, self.secret_env_names):
            return REDACTED_VALUE
        if isinstance(value, dict):
            return {
                str(item_key): self.sanitize_object(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self.sanitize_object(item, key=key) for item in value]
        if isinstance(value, str):
            return self.sanitize_text(value)
        return value

    def build_sandbox_env(self, allowlist=(), extra=None):
        extra = dict(extra or {})
        result = {
            name: self.sanitize_text(self.env[name])
            for name in allowlist
            if name in self.env and not is_secret_env_name(name, self.secret_env_names)
        }
        for name, value in extra.items():
            if not is_secret_env_name(name, self.secret_env_names):
                result[str(name)] = self.sanitize_text(value)
        return result


def _normalized_secret_names(secret_env_names):
    return {str(name).upper() for name in (secret_env_names or ())}


def looks_sensitive_env_name(name):
    upper = str(name).upper()
    return upper.endswith(tuple(SENSITIVE_ENV_NAME_MARKERS))


def is_secret_env_name(name, secret_env_names=None):
    upper = str(name).upper()
    return upper in _normalized_secret_names(secret_env_names) or looks_sensitive_env_name(upper)


def configured_secret_env_items(env=None, secret_env_names=None):
    env = os.environ if env is None else env
    configured_names = _normalized_secret_names(secret_env_names)
    items = [
        (name, value)
        for name, value in env.items()
        if str(name).upper() in configured_names and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def detected_secret_env_items(env=None, secret_env_names=None):
    env = os.environ if env is None else env
    items = [
        (name, value)
        for name, value in env.items()
        if is_secret_env_name(name, secret_env_names=secret_env_names) and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def secret_env_summary(env=None, secret_env_names=None):
    names = [name for name, _ in configured_secret_env_items(env=env, secret_env_names=secret_env_names)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def detected_secret_env_summary(env=None, secret_env_names=None):
    names = [name for name, _ in detected_secret_env_items(env=env, secret_env_names=secret_env_names)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def redact_text(text, env=None, secret_env_names=None):
    text = str(text)
    for _, value in sorted(
        detected_secret_env_items(env=env, secret_env_names=secret_env_names),
        key=lambda item: len(item[1]),
        reverse=True,
    ):
        text = text.replace(value, REDACTED_VALUE)
    return text


def redact_artifact(value, key=None, env=None, secret_env_names=None):
    if key and is_secret_env_name(key, secret_env_names=secret_env_names):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return {
            str(item_key): redact_artifact(item_value, key=item_key, env=env, secret_env_names=secret_env_names)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_artifact(item, key=key, env=env, secret_env_names=secret_env_names) for item in value]
    if isinstance(value, tuple):
        return [redact_artifact(item, key=key, env=env, secret_env_names=secret_env_names) for item in value]
    if isinstance(value, str):
        return redact_text(value, env=env, secret_env_names=secret_env_names)
    return value


def shell_env(env=None, allowlist=(), root="."):
    env = os.environ if env is None else env
    filtered = {
        name: env[name]
        for name in allowlist
        if name in env
    }
    filtered["PWD"] = str(root)
    if "PATH" not in filtered and env.get("PATH"):
        filtered["PATH"] = env["PATH"]
    return filtered
