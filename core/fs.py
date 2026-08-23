"""Small tested filesystem primitives for private runtime state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Write a complete file through fsync and same-directory atomic replace."""
    destination = Path(path)
    if destination.is_symlink():
        raise OSError("refusing to replace a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temp_path = Path(raw_temp)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        if mode is not None:
            destination.chmod(mode)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(
    path: str | Path,
    payload: dict[str, Any],
    *,
    mode: int | None = None,
    trailing_newline: bool = False,
) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True)
    if trailing_newline:
        content += "\n"
    atomic_write_text(path, content, mode=mode)


def read_json_object(
    path: str | Path,
    *,
    default: dict[str, Any] | None = None,
    max_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Read one bounded, non-symlink JSON object or return a copied default."""
    source = Path(path)
    fallback = dict(default or {})
    if not source.exists():
        return fallback
    if source.is_symlink() or not source.is_file():
        raise OSError("JSON state path must be a regular non-symlink file")
    raw = source.read_bytes()
    if len(raw) > max(1, int(max_bytes)):
        raise OSError("JSON state exceeds the size limit")
    loaded = json.loads(raw.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("JSON state must contain an object")
    return loaded
