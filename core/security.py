"""Small, dependency-free security primitives shared across LightClaw."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

SAFE_DELEGATED_ENV_KEYS = frozenset(
    {
        "COLORTERM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)

SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"bot[_-]?token|password|passwd|secret|credential|private[_-]?key)"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Z0-9_.-])([\"']?)([A-Z0-9_.-]{0,128}(?:API[_-]?KEY|ACCESS[_-]?KEY|AUTH[_-]?TOKEN|"
    r"REFRESH[_-]?TOKEN|BOT[_-]?TOKEN|TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|PRIVATE[_-]?KEY)"
    r"[A-Z0-9_.-]{0,128})([\"']?)(\s*[:=]\s*)"
    r"([\"']?)([^\s,;}\"']+)([\"']?)"
)
SENSITIVE_PATH_DIRECTORIES = frozenset({".aws", ".docker", ".gnupg", ".kube", ".ssh", "credentials", "secrets"})
SENSITIVE_PATH_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "application_default_credentials.json",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
SENSITIVE_KEY_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pem", ".pfx")
SENSITIVE_DATA_SUFFIXES = (".conf", ".config", ".ini", ".json", ".toml", ".yaml", ".yml")
SENSITIVE_PATH_MARKER_RE = re.compile(
    r"(?i)(?:^|[._-])(?:api[_-]?keys?|access[_-]?keys?|auth(?:[_-]?token)?|access[_-]?token|"
    r"refresh[_-]?token|bot[_-]?token|credentials?|password|passwd|secrets?|token|"
    r"private[_-]?keys?)(?:$|[._-])"
)
BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")


def delegated_process_env(
    source: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment for local coding-agent subprocesses."""
    values = os.environ if source is None else source
    result = {
        key: str(values[key])
        for key in SAFE_DELEGATED_ENV_KEYS
        if key in values and str(values[key]).strip()
    }
    result.setdefault("PATH", os.defpath)
    result.setdefault("LANG", "C.UTF-8")
    result["LIGHTCLAW_DELEGATED"] = "1"
    if extra:
        for key, value in extra.items():
            if SENSITIVE_NAME_RE.search(key):
                continue
            result[str(key)] = str(value)
    return result


def redact_text(text: str, known_values: Mapping[str, str] | None = None) -> str:
    """Redact common credentials and explicitly known sensitive values."""
    cleaned = str(text or "")
    if known_values:
        for key, value in known_values.items():
            secret = str(value or "")
            if not secret or not SENSITIVE_NAME_RE.search(str(key)):
                continue
            if len(secret) >= 4:
                cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2\3\4\5[REDACTED]\7", cleaned)
    cleaned = BEARER_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = TELEGRAM_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", cleaned)
    return cleaned


def is_sensitive_path(path: str) -> bool:
    """Recognize common credential paths before including file contents in prompts."""
    parts = [part.lower() for part in str(path).replace("\\", "/").split("/") if part]
    if any(part in SENSITIVE_PATH_DIRECTORIES for part in parts):
        return True
    name = parts[-1] if parts else ""
    if (
        name in SENSITIVE_PATH_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or name.endswith(SENSITIVE_KEY_SUFFIXES)
    ):
        return True
    return bool(
        name.endswith(SENSITIVE_DATA_SUFFIXES) and SENSITIVE_PATH_MARKER_RE.search(name)
    )


def has_sensitive_content(text: str) -> bool:
    """Return True when current credential redaction rules detect secret-like text."""
    source = str(text or "")
    return redact_text(source) != source


def access_policy_label(allowed_users: list[str], public_ack: bool) -> str:
    if allowed_users:
        return f"restricted ({len(allowed_users)} allowed user(s); private chats only)"
    if public_ack:
        return "public (explicit override)"
    return "blocked (no owner configured)"
