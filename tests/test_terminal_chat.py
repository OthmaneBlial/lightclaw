from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot
from lightclaw_cli import cmd_chat
from memory import MemoryStore


@pytest.mark.parametrize("allowed_users", [[], ["42"]])
def test_terminal_commands_use_local_authority_and_keep_cli_memory_scope(
    tmp_path, monkeypatch, capsys, allowed_users
):
    config = Config(
        llm_provider="fixture",
        workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"),
        skills_state_path=str(tmp_path / "skills.json"),
        telegram_allowed_users=allowed_users,
    )
    user = allowed_users[0] if allowed_users else "cli-user"
    namespace = f"cli-user:{user}"
    workspace = str((tmp_path / "workspace").resolve())
    bots = []

    def create_bot(config):
        bot = LightClawBot(config)
        bot.memory.bind_session(
            "terminal", user_namespace=namespace, workspace_namespace=workspace
        )
        bot.memory.ingest("user", "terminal secret amberfalcon <draft> & review", "terminal")
        bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
        bot._plan_multi_agent_payload = AsyncMock(return_value=({
            "goal": "inspect docs",
            "workers": [("builder", "codex"), ("reviewer", "codex")],
            "plan_payload": {"workers": [{"label": "builder", "owned_paths": ["docs/"]}]},
        }, ""))
        bot._execute_multi_agent_plan = AsyncMock()
        bots.append(bot)
        return bot

    monkeypatch.setattr("lightclaw_cli._prepare_runtime_environment", lambda *_a, **_k: 0)
    monkeypatch.setattr("config.load_config", lambda: config)
    monkeypatch.setattr("main.LightClawBot", create_bot)
    monkeypatch.setattr(
        "core.bot.base.LLMClient", lambda _config: SimpleNamespace(close=Mock(), chat=AsyncMock())
    )
    monkeypatch.setenv("LIGHTCLAW_CHAT_MODE", "0")
    lines = iter([
        "/help", "/recall amberfalcon", "/agent multi inspect docs", "yes",
        "/agent trusted codex inspect external files", "/exit"
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(lines))

    assert cmd_chat(SimpleNamespace(home=str(tmp_path), provider="", model="", session="terminal")) == 0

    output = capsys.readouterr().out
    assert "LightClaw Commands" in output
    assert "terminal secret amberfalcon <draft> & review" in output
    assert "Trusted host execution requested" in output
    assert "Command failed" not in output
    assert config.telegram_allowed_users == allowed_users
    assert not config.telegram_public_bot_ack
    bots[0].llm.chat.assert_not_awaited()
    bots[0]._execute_multi_agent_plan.assert_awaited_once()
    assert bots[0]._execute_multi_agent_plan.await_args.kwargs["goal"] == "inspect docs"

    memory = MemoryStore(tmp_path / "memory.db")
    try:
        assert memory.scope_for("terminal") == (namespace, workspace)
    finally:
        memory.db.close()
