from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from core.fs import (
    FileTooLargeError,
    atomic_write_json,
    atomic_write_text,
    atomic_write_text_at,
    read_json_object,
    read_text_bounded,
    read_text_bounded_at,
    sha256_file,
)


class _ReadProbe:
    def __init__(self, handle, requests):
        self.handle = handle
        self.requests = requests

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def read(self, size=-1):
        self.requests.append(size)
        return self.handle.read(size)


def test_sha256_file_reads_large_files_in_bounded_chunks(tmp_path, monkeypatch):
    path = tmp_path / "large.bin"
    chunk = b"x" * (1024 * 1024)
    expected = hashlib.sha256()
    with path.open("wb") as handle:
        for _ in range(3):
            handle.write(chunk)
            expected.update(chunk)

    read_requests = []
    original_open = Path.open

    def probe_open(candidate, *args, **kwargs):
        handle = original_open(candidate, *args, **kwargs)
        return _ReadProbe(handle, read_requests) if candidate == path else handle

    monkeypatch.setattr(Path, "open", probe_open)
    assert sha256_file(path) == expected.hexdigest()
    assert read_requests and max(read_requests) <= 1024 * 1024


def test_bounded_text_reader_stops_at_limit_plus_one(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_text("x" * 100, encoding="utf-8")
    read_requests = []
    original_open = Path.open

    def probe_open(candidate, *args, **kwargs):
        handle = original_open(candidate, *args, **kwargs)
        return _ReadProbe(handle, read_requests) if candidate == path else handle

    monkeypatch.setattr(Path, "open", probe_open)
    with pytest.raises(FileTooLargeError):
        read_text_bounded(path, 8)
    assert read_requests == [9]


def test_atomic_private_write_and_bounded_json_read(tmp_path):
    destination = tmp_path / "state" / "value.json"
    atomic_write_json(
        destination,
        {"value": 3},
        mode=0o600,
        trailing_newline=True,
    )

    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.read_text(encoding="utf-8").endswith("\n")
    assert read_json_object(destination) == {"value": 3}


def test_atomic_write_refuses_symlink_and_cleans_failed_temp(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    link = tmp_path / "linked.txt"
    link.symlink_to(target)
    with pytest.raises(OSError, match="symlink"):
        atomic_write_text(link, "changed")
    assert target.read_text(encoding="utf-8") == "original"

    def _fail_replace(_source, _destination):
        raise OSError("fixture replace failure")

    monkeypatch.setattr("core.fs.os.replace", _fail_replace)
    with pytest.raises(OSError, match="fixture replace failure"):
        atomic_write_text(tmp_path / "failed.txt", "value")
    assert not list(tmp_path.glob(".failed.txt.*.tmp"))


def test_atomic_write_does_not_chmod_destination_swapped_to_symlink(tmp_path, monkeypatch):
    destination = tmp_path / "value.txt"
    victim = tmp_path / "victim.txt"
    victim.write_text("private", encoding="utf-8")
    os.chmod(victim, 0o644)
    replace = os.replace

    def replace_then_swap(source, target):
        replace(source, target)
        Path(target).unlink()
        Path(target).symlink_to(victim)

    monkeypatch.setattr("core.fs.os.replace", replace_then_swap)
    atomic_write_text(destination, "value", mode=0o600)

    assert victim.stat().st_mode & 0o777 == 0o644


def test_workspace_text_io_rejects_symlinked_parent(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "value.txt").write_text("safe", encoding="utf-8")
    (workspace / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        read_text_bounded_at(workspace, "nested/value.txt", 100)
    with pytest.raises(OSError):
        atomic_write_text_at(workspace, "nested/value.txt", "changed")

    assert (outside / "value.txt").read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_workspace_bounded_reader_rejects_named_pipe(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")

    with pytest.raises(OSError, match="regular file"):
        read_text_bounded_at(workspace, "pipe", 100)


def test_json_reader_rejects_dangling_symlink_instead_of_using_default(tmp_path):
    target = tmp_path / "missing-state.json"
    linked = tmp_path / "state.json"
    linked.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        read_json_object(linked, default={"jobs": []})

    assert not target.exists()


def test_json_reader_rejects_oversize_and_non_object(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        read_json_object(path)
    path.write_text('{"long":"value"}', encoding="utf-8")

    read_requests = []
    original_open = Path.open

    def probe_open(candidate, *args, **kwargs):
        handle = original_open(candidate, *args, **kwargs)
        return _ReadProbe(handle, read_requests) if candidate == path else handle

    monkeypatch.setattr(Path, "open", probe_open)
    with pytest.raises(OSError, match="size limit"):
        read_json_object(path, max_bytes=4)
    assert read_requests == [5]
