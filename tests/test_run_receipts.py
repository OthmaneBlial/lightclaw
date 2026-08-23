from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from core.bot import LightClawBot


def test_real_delegation_path_emits_private_structured_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-receipt-fixture")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(
        workspace_path=str(tmp_path / "workspace"),
        local_agent_timeout_sec=30,
        local_agent_progress_interval_sec=10,
        local_agent_capability_profile="workspace-write",
    )
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._delegation_safety_block_reason = lambda _task: ""

    async def fake_invoke(**kwargs):
        workspace = Path(kwargs["workspace"])
        (workspace / "result.txt").write_text("verified\n", encoding="utf-8")
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "summary": "Created a verified result without sk-receipt-fixture",
            "elapsed": 0.25,
            "timed_out": False,
        }

    bot._invoke_local_agent_streaming = fake_invoke
    evidence: dict[str, object] = {}
    result = asyncio.run(
        bot._run_local_agent_task(
            session_id="fixture-session",
            agent="codex",
            task="Create result.txt; credential=sk-receipt-fixture",
            evidence_sink=evidence,
        )
    )

    receipt_path = Path(str(evidence["receipt_json"]))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["disposition"] == "ready_for_review"
    assert receipt["plan"][0]["worker"] == "codex"
    assert receipt["commands"][0]["exit_code"] == 0
    assert receipt["file_changes"][0]["path"] == "result.txt"
    assert receipt["file_changes"][0]["change"] == "created"
    assert receipt["checks"][0]["passed"] is True
    assert receipt["checkpoint"]["is_git"] is False
    assert "[REDACTED]" in receipt["original_goal"]
    assert "sk-receipt-fixture" not in receipt_path.read_text(encoding="utf-8")
    assert "Receipt:" in result
