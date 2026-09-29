from __future__ import annotations

import json

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
    assert any("non-symlink" in failure for failure in failures)
