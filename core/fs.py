"""Small tested filesystem primitives for private runtime state."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


class FileTooLargeError(OSError):
    """Raised when a bounded file operation exceeds its byte limit."""


def secure_sqlite_files(path: str | Path) -> None:
    """Restrict a SQLite database and any existing WAL sidecars to the owner."""
    database = Path(path)
    nofollow = os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        database_fd = os.open(database, os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    except FileExistsError:
        database_fd = os.open(database, os.O_RDONLY | nofollow)

    try:
        _secure_open_sqlite_file(database, database_fd)
    finally:
        os.close(database_fd)

    for candidate in (Path(f"{database}-wal"), Path(f"{database}-shm")):
        try:
            sidecar_fd = os.open(candidate, os.O_RDONLY | nofollow)
        except FileNotFoundError:
            continue
        try:
            _secure_open_sqlite_file(candidate, sidecar_fd)
        finally:
            os.close(sidecar_fd)


def _secure_open_sqlite_file(path: Path, fd: int) -> None:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(errno.EINVAL, f"SQLite state path is not a regular file: {path}")
    os.fchmod(fd, 0o600)
    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise OSError(errno.EINVAL, f"SQLite state path changed during permission update: {path}")


def sha256_file(path: str | Path) -> str:
    """Hash a file in fixed-size chunks so large artifacts stay memory-bounded."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_bounded(
    path: str | Path,
    max_bytes: int,
    *,
    encoding: str = "utf-8",
) -> str:
    """Read and decode text while consuming at most max_bytes plus one byte."""
    limit = max(1, int(max_bytes))
    with Path(path).open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise FileTooLargeError(f"file exceeds the {limit}-byte read limit")
    return raw.decode(encoding)


def _relative_parts(relative: str | Path) -> tuple[str, ...]:
    path = PurePosixPath(str(relative))
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ValueError("workspace path must be safe and relative")
    return path.parts


def open_directory_at(
    root: str | Path | int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
    private: bool = False,
) -> int:
    """Open a workspace directory by components without following symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd: int | None = None
    try:
        directory_fd = os.dup(root) if isinstance(root, int) else os.open(root, flags)
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise OSError(errno.ENOTDIR, "workspace root is not a directory")
        for part in parts:
            if part in {"", ".", ".."} or "/" in part or "\x00" in part:
                raise ValueError("workspace path must be safe and relative")
            if create:
                try:
                    os.mkdir(part, 0o700 if private else 0o777, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            try:
                if private:
                    os.fchmod(child_fd, 0o700)
            except OSError:
                os.close(child_fd)
                raise
            previous_fd = directory_fd
            directory_fd = child_fd
            os.close(previous_fd)
        return directory_fd
    except BaseException as exc:
        if directory_fd is not None:
            os.close(directory_fd)
        if isinstance(exc, OSError) and exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise OSError(
                exc.errno, "workspace path contains a symlink or non-directory parent"
            ) from exc
        raise


def open_regular_file_at(
    root: str | Path | int, relative: str | Path
) -> tuple[int, os.stat_result]:
    """Open one regular workspace file without following symlinks."""
    parts = _relative_parts(relative)
    directory_fd = open_directory_at(root, parts[:-1])
    file_fd: int | None = None
    try:
        file_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(errno.EINVAL, "workspace file is not a regular file")
        return file_fd, file_stat
    except OSError as exc:
        if file_fd is not None:
            os.close(file_fd)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise OSError(
                exc.errno, "workspace file contains a symlink or non-regular path"
            ) from exc
        raise
    finally:
        os.close(directory_fd)


def read_text_bounded_at(
    root: str | Path | int,
    relative: str | Path,
    max_bytes: int,
    *,
    encoding: str = "utf-8",
) -> str:
    """Read a bounded text file through a symlink-safe workspace descriptor."""
    file_fd, _ = open_regular_file_at(root, relative)
    limit = max(1, int(max_bytes))
    with os.fdopen(file_fd, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise FileTooLargeError(f"file exceeds the {limit}-byte read limit")
    return bytes(raw).decode(encoding)


def _workspace_file_snapshot_at(
    directory_fd: int, name: str, expected_content: str | None, encoding: str
) -> tuple[int, tuple[int, int, int, int, int] | None]:
    try:
        file_fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
    except FileNotFoundError:
        if expected_content is None:
            return 0o600, None
        raise OSError(errno.EAGAIN, "workspace file changed since it was read") from None

    try:
        current = os.fstat(file_fd)
        if not stat.S_ISREG(current.st_mode):
            raise OSError(errno.EINVAL, "workspace target is not a regular file")
        if expected_content is None:
            raise FileExistsError(errno.EEXIST, "workspace file appeared after it was read")
        expected_bytes = expected_content.encode(encoding)
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            if handle.read(len(expected_bytes) + 1) != expected_bytes:
                raise OSError(errno.EAGAIN, "workspace file changed since it was read")
        return current.st_mode & 0o777, (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_mode & 0o777,
        )
    finally:
        if file_fd is not None:
            os.close(file_fd)


def atomic_write_text_at(
    root: str | Path | int,
    relative: str | Path,
    content: str,
    *,
    expected_content: str | None,
    private_parents: bool = False,
    mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Atomically write beneath a workspace using symlink-safe directory handles."""
    parts = _relative_parts(relative)
    directory_fd = open_directory_at(
        root, parts[:-1], create=True, private=private_parents
    )
    name = parts[-1]
    temp_name: str | None = None
    file_fd: int | None = None
    try:
        initial_mode, initial_snapshot = _workspace_file_snapshot_at(
            directory_fd, name, expected_content, encoding
        )
        write_mode = initial_mode if mode is None else mode

        temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
        file_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(file_fd, "w", encoding=encoding) as handle:
            file_fd = None
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), write_mode)
            os.fsync(handle.fileno())

        current_mode, current_snapshot = _workspace_file_snapshot_at(
            directory_fd, name, expected_content, encoding
        )
        if current_snapshot != initial_snapshot or current_mode != initial_mode:
            raise OSError(errno.EAGAIN, "workspace file changed while the update was prepared")

        if expected_content is None:
            os.link(
                temp_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temp_name, dir_fd=directory_fd)
        else:
            os.replace(
                temp_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        temp_name = None
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


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
    max_bytes: int | None = None,
) -> None:
    content = json.dumps(payload, indent=2, sort_keys=True)
    if trailing_newline:
        content += "\n"
    if max_bytes is not None and len(content.encode("utf-8")) > max(1, int(max_bytes)):
        raise FileTooLargeError("JSON state exceeds the size limit")
    atomic_write_text(path, content, mode=mode)


def read_json_object(
    path: str | Path,
    *,
    default: dict[str, Any] | None = None,
    max_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    """Read one bounded, non-symlink JSON object or return a copied default."""
    source = Path(path)
    try:
        content = read_text_bounded_at(source.parent.resolve(), source.name, max_bytes)
    except FileNotFoundError:
        return dict(default or {})
    loaded = json.loads(content)
    if not isinstance(loaded, dict):
        raise ValueError("JSON state must contain an object")
    return loaded
