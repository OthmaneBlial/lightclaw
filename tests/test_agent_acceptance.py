from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.bot import LightClawBot


def _acceptance(bot: LightClawBot, workspace):
    return bot._evaluate_multi_worker_acceptance(
        workspace,
        "builder",
        {
            "role": "implementation",
            "owned_paths": ["README.md"],
            "acceptance_checks": [
                {"type": "handoff_json", "path": "handoff/builder.json"},
                {"type": "json_field_nonempty", "field": "summary"},
            ],
        },
    )


def test_handoff_acceptance_rejects_json_over_one_mib(tmp_path):
    handoff = tmp_path / "handoff" / "builder.json"
    handoff.parent.mkdir()
    handoff.write_bytes(
        b'{"lane":"builder","summary":"'
        + b"x" * (1024 * 1024)
        + b'","changed_files":[]}'
    )
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = _acceptance(bot, tmp_path)

    assert not passed
    assert any("exceeds 1 MiB" in failure for failure in failures)


def test_handoff_acceptance_rejects_symlinked_json(tmp_path):
    handoff = tmp_path / "handoff" / "builder.json"
    handoff.parent.mkdir()
    secret_json = tmp_path / "outside.json"
    secret_json.write_text(
        json.dumps({"lane": "builder", "summary": "outside", "changed_files": []}),
        encoding="utf-8",
    )
    handoff.symlink_to(secret_json)
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = _acceptance(bot, tmp_path)

    assert not passed
    assert any("symlink" in failure for failure in failures)


def test_acceptance_command_rejects_cwd_symlink_outside_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr("core.bot.commands.agent_acceptance.subprocess.run", run)
    bot = LightClawBot.__new__(LightClawBot)

    failure = bot._run_multi_acceptance_command(
        workspace, {"command": "python -c pass", "cwd": "external"}
    )

    assert "outside the workspace" in failure
    run.assert_not_called()


def test_handoff_acceptance_rejects_symlinked_parent_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "builder.json").write_text(
        json.dumps({"lane": "builder", "summary": "outside", "changed_files": []}),
        encoding="utf-8",
    )
    (workspace / "handoff").symlink_to(outside, target_is_directory=True)
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = _acceptance(bot, workspace)

    assert not passed
    assert failures


@pytest.mark.parametrize(
    "check",
    [
        {"type": "file_exists", "path": "external/secret.txt"},
        {"type": "glob_nonempty", "pattern": "external/*.txt"},
    ],
)
def test_acceptance_checks_reject_paths_through_external_symlink(tmp_path, check):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = bot._evaluate_multi_worker_acceptance(
        workspace,
        "builder",
        {"role": "implementation", "owned_paths": [], "acceptance_checks": [check]},
    )

    assert not passed
    assert failures


def test_reported_files_reject_paths_through_external_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "handoff").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "external").symlink_to(outside, target_is_directory=True)
    (workspace / "handoff" / "builder.json").write_text(
        json.dumps(
            {"lane": "builder", "summary": "done", "changed_files": ["external/secret.txt"]}
        ),
        encoding="utf-8",
    )
    bot = LightClawBot.__new__(LightClawBot)

    passed, failures, _ = bot._evaluate_multi_worker_acceptance(
        workspace,
        "builder",
        {
            "role": "implementation",
            "owned_paths": [],
            "acceptance_checks": [
                {"type": "handoff_json", "path": "handoff/builder.json"},
                {"type": "reported_files_exist"},
            ],
        },
    )

    assert not passed
    assert failures
