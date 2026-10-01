from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot
from lightclaw_cli import cmd_chat
from memory import MemoryStore


@pytest.mark.parametrize("stage", ["bind", "input", "model", "exit"])
def test_terminal_closes_resources_on_every_exit(tmp_path, monkeypatch, stage):
    config = Config(
        llm_provider="fixture", workspace_path=str(tmp_path / "workspace"),
        memory_db_path=str(tmp_path / "memory.db"), skills_state_path=str(tmp_path / "skills.json"),
    )
    error = {
        "bind": RuntimeError("bind failed"), "input": OSError("input failed"),
        "model": KeyboardInterrupt("model interrupted"), "exit": None,
    }[stage]
    llm = SimpleNamespace(close=Mock(), chat=AsyncMock(side_effect=error))
    monkeypatch.setattr("core.bot.base.LLMClient", lambda _config: llm)
    monkeypatch.setattr("lightclaw_cli._prepare_runtime_environment", lambda *_a, **_k: 0)
    monkeypatch.setattr("config.load_config", lambda: config)
    monkeypatch.setenv("LIGHTCLAW_CHAT_MODE", "0")
    bots = []
    memory_read_threads = []
    loop_thread = threading.get_ident()

    def create_bot(config):
        bot = LightClawBot(config)
        bots.append(bot)
        for method_name in ("recall", "get_recent"):
            original = getattr(bot.memory, method_name)

            def record_thread(*args, _original=original, _name=method_name, **kwargs):
                memory_read_threads.append((_name, threading.get_ident()))
                return _original(*args, **kwargs)

            setattr(bot.memory, method_name, record_thread)
        if stage == "bind":
            bot.memory.bind_session = Mock(side_effect=error)
        return bot

    def read_input(_prompt):
        if stage == "input":
            raise error
        return "/exit" if stage == "exit" else "hello"

    monkeypatch.setattr("main.LightClawBot", create_bot)
    monkeypatch.setattr("builtins.input", read_input)
    args = SimpleNamespace(home=str(tmp_path), provider="", model="", session="terminal")
    try:
        if error is None:
            assert cmd_chat(args) == 0
        else:
            with pytest.raises(type(error), match=str(error)):
                cmd_chat(args)
        llm.close.assert_called_once_with()
        for database in (bots[0].memory.db, bots[0].jobs.db):
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                database.execute("SELECT 1")
        if stage == "model":
            assert {name for name, _thread in memory_read_threads} == {"recall", "get_recent"}
            assert all(thread != loop_thread for _name, thread in memory_read_threads)
    finally:
        for bot in bots:
            bot.memory.db.close()
            bot.jobs.close()


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
