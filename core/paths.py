"""Canonical LightClaw configuration and runtime paths."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "lightclaw"
CONFIG_FILENAME = "config.env"
LEGACY_CONFIG_FILENAME = ".env"
RUNTIME_DIRNAME = ".lightclaw"


def resolve_home(home: str | Path | None = None) -> Path:
    """Resolve an explicit runtime home or the current user's home."""
    if home is not None and str(home).strip():
        return Path(home).expanduser().resolve()
    return Path.home().resolve()


def config_dir(home: str | Path | None = None) -> Path:
    """Return the app-specific configuration directory."""
    resolved_home = resolve_home(home)
    if home is None:
        xdg = os.getenv("XDG_CONFIG_HOME", "").strip()
        if xdg:
            return Path(xdg).expanduser().resolve() / APP_NAME
    return resolved_home / ".config" / APP_NAME


def config_path(home: str | Path | None = None) -> Path:
    override = os.getenv("LIGHTCLAW_CONFIG", "").strip()
    if override and home is None:
        return Path(override).expanduser().resolve()
    return config_dir(home) / CONFIG_FILENAME


def legacy_config_path(home: str | Path | None = None) -> Path:
    return resolve_home(home) / LEGACY_CONFIG_FILENAME


def runtime_dir(home: str | Path | None = None) -> Path:
    return resolve_home(home) / RUNTIME_DIRNAME


def runtime_workspace(home: str | Path | None = None) -> Path:
    return runtime_dir(home) / "workspace"
