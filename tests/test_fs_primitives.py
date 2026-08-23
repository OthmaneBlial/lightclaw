from __future__ import annotations

import json

import pytest

from core.fs import atomic_write_json, atomic_write_text, read_json_object


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


def test_json_reader_rejects_oversize_and_non_object(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        read_json_object(path)
    path.write_text('{"long":"value"}', encoding="utf-8")
    with pytest.raises(OSError, match="size limit"):
        read_json_object(path, max_bytes=4)
