#!/usr/bin/env python3
"""Verify declared LightClaw data files in an installed wheel environment."""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from sysconfig import get_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_FILES = (
    Path("share/lightclaw/.env.example"),
    Path("share/lightclaw/templates/personality/HEARTBEAT.md"),
    Path("share/lightclaw/templates/personality/IDENTITY.md"),
    Path("share/lightclaw/templates/personality/SOUL.md"),
    Path("share/lightclaw/templates/personality/USER.md"),
)


def main() -> int:
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or PROJECT_ROOT in prefix.parents:
        print("Wheel data probe must run in a temporary environment outside the source checkout.")
        return 1
    try:
        distribution("lightclaw-ai")
    except PackageNotFoundError:
        print("Installed distribution not found: lightclaw-ai")
        return 1

    data_root = Path(get_path("data"))
    missing = [
        relative
        for relative in DATA_FILES
        if not (data_root / relative).is_file() or (data_root / relative).stat().st_size == 0
    ]
    if missing:
        for relative in missing:
            print(f"Missing or empty wheel data file: {relative.as_posix()}")
        return 1

    print(f"Wheel data files verified: {len(DATA_FILES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
