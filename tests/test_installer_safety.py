from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lightclaw_cli import _managed_uninstall_targets, cmd_uninstall

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_MARKER = "managed-by=lightclaw\n"


def _run_setup(script: Path, home: Path, install_root: Path | str):
    environment = os.environ.copy()
    environment.update(
        HOME=str(home),
        LIGHTCLAW_INSTALL_ROOT=str(install_root),
        LIGHTCLAW_SKIP_ONBOARD="yes",
    )
    return subprocess.run(
        ["bash", str(script)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_setup_refuses_to_claim_existing_unmarked_data(tmp_path):
    home = tmp_path / "home"
    install_root = home / ".local" / "share" / "lightclaw"
    install_root.mkdir(parents=True)
    user_data = install_root / "notes.txt"
    user_data.write_text("keep this", encoding="utf-8")

    result = _run_setup(PROJECT_ROOT / "setup.sh", home, install_root)

    assert result.returncode != 0
    assert "not marked as a LightClaw install" in result.stderr
    assert user_data.read_text(encoding="utf-8") == "keep this"
    assert not (install_root / ".lightclaw-install").exists()
    assert not (install_root / "venv").exists()


def test_setup_rejects_symlink_install_root_with_trailing_slash(tmp_path):
    home = tmp_path / "home"
    victim = tmp_path / "valuable"
    victim.mkdir()
    (victim / ".lightclaw-install").write_text(INSTALL_MARKER, encoding="utf-8")
    (victim / "notes.txt").write_text("keep this", encoding="utf-8")
    install_link = home / ".local" / "share" / "lightclaw"
    install_link.parent.mkdir(parents=True)
    install_link.symlink_to(victim, target_is_directory=True)

    result = _run_setup(
        PROJECT_ROOT / "setup.sh", home, f"{install_link}/"
    )

    assert result.returncode != 0
    assert "is a symlink" in result.stderr
    assert (victim / "notes.txt").read_text(encoding="utf-8") == "keep this"
    assert not (victim / "venv").exists()


def test_remote_setup_refuses_unverified_source_checkout(tmp_path):
    home = tmp_path / "home"
    install_root = home / ".local" / "share" / "lightclaw"
    source = install_root / "source"
    (source / ".git").mkdir(parents=True)
    (install_root / ".lightclaw-install").write_text(INSTALL_MARKER, encoding="utf-8")
    bootstrap = tmp_path / "downloaded" / "setup.sh"
    bootstrap.parent.mkdir()
    shutil.copy2(PROJECT_ROOT / "setup.sh", bootstrap)

    result = _run_setup(bootstrap, home, install_root)

    assert result.returncode != 0
    assert "not a valid managed LightClaw checkout" in result.stderr
    assert (install_root / ".lightclaw-install").read_text(encoding="utf-8") == INSTALL_MARKER
    assert not (install_root / "venv").exists()


def test_remote_setup_refuses_symlink_git_metadata(tmp_path):
    home = tmp_path / "home"
    install_root = home / ".local" / "share" / "lightclaw"
    source = install_root / "source"
    source.mkdir(parents=True)
    (install_root / ".lightclaw-install").write_text(INSTALL_MARKER, encoding="utf-8")
    outside_repo = tmp_path / "outside-repo"
    subprocess.run(["git", "init", str(outside_repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(outside_repo), "remote", "add", "origin", "https://github.com/OthmaneBlial/lightclaw.git"],
        check=True,
        capture_output=True,
    )
    (source / ".git").symlink_to(outside_repo / ".git", target_is_directory=True)
    bootstrap = tmp_path / "downloaded" / "setup.sh"
    bootstrap.parent.mkdir()
    shutil.copy2(PROJECT_ROOT / "setup.sh", bootstrap)

    result = _run_setup(bootstrap, home, install_root)

    assert result.returncode != 0
    assert ".git is a symlink" in result.stderr
    assert not (install_root / "venv").exists()


def test_setup_refuses_venv_symlink_before_running_its_python(tmp_path):
    home = tmp_path / "home"
    install_root = home / ".local" / "share" / "lightclaw"
    install_root.mkdir(parents=True)
    (install_root / ".lightclaw-install").write_text(INSTALL_MARKER, encoding="utf-8")
    outside_venv = tmp_path / "outside-venv"
    python = outside_venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    marker = tmp_path / "python-ran"
    python.write_text(f"#!/bin/sh\nprintf ran > {marker}\n", encoding="utf-8")
    python.chmod(0o755)
    (install_root / "venv").symlink_to(outside_venv, target_is_directory=True)

    result = _run_setup(PROJECT_ROOT / "setup.sh", home, install_root)

    assert result.returncode != 0
    assert "is a symlink" in result.stderr
    assert not marker.exists()
    assert python.is_file()


@pytest.mark.parametrize("marker", ["user notes\n", INSTALL_MARKER + "extra\n"])
def test_uninstall_preserves_directory_with_unverified_marker(tmp_path, monkeypatch, marker):
    install_root = tmp_path / ".local" / "share" / "lightclaw"
    install_root.mkdir(parents=True)
    (install_root / ".lightclaw-install").write_text(marker, encoding="utf-8")
    user_data = install_root / "notes.txt"
    user_data.write_text("keep this", encoding="utf-8")
    monkeypatch.setenv("LIGHTCLAW_INSTALL_ROOT", str(install_root))

    code = cmd_uninstall(
        SimpleNamespace(home=str(tmp_path), purge_data=False, apply=True, yes=False)
    )

    assert code == 0
    assert user_data.read_text(encoding="utf-8") == "keep this"
    assert install_root.is_dir()
    assert not any(path == install_root for path, _ in _managed_uninstall_targets(tmp_path))


def test_uninstall_rejects_symlink_marker_and_install_root(tmp_path, monkeypatch):
    victim = tmp_path / "valuable"
    victim.mkdir()
    (victim / ".lightclaw-install").write_text(INSTALL_MARKER, encoding="utf-8")
    (victim / "notes.txt").write_text("keep this", encoding="utf-8")
    install_link = tmp_path / "install-link"
    install_link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setenv("LIGHTCLAW_INSTALL_ROOT", str(install_link))

    assert _managed_uninstall_targets(tmp_path) == []
    assert cmd_uninstall(
        SimpleNamespace(home=str(tmp_path), purge_data=False, apply=True, yes=False)
    ) == 0
    assert (victim / "notes.txt").read_text(encoding="utf-8") == "keep this"


def test_uninstall_keeps_unverified_command_symlink(tmp_path, monkeypatch):
    install_root = tmp_path / ".local" / "share" / "lightclaw"
    custom_tool = install_root / "custom" / "lightclaw"
    custom_tool.parent.mkdir(parents=True)
    custom_tool.write_text("user command", encoding="utf-8")
    (install_root / ".lightclaw-install").write_text("user marker\n", encoding="utf-8")
    command = tmp_path / ".local" / "bin" / "lightclaw"
    command.parent.mkdir(parents=True)
    command.symlink_to(custom_tool)
    monkeypatch.setenv("LIGHTCLAW_INSTALL_ROOT", str(install_root))

    assert _managed_uninstall_targets(tmp_path) == []
    assert cmd_uninstall(
        SimpleNamespace(home=str(tmp_path), purge_data=False, apply=True, yes=False)
    ) == 0
    assert command.is_symlink()
    assert custom_tool.read_text(encoding="utf-8") == "user command"
