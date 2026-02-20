"""Application entrypoint and Telegram handler registration."""

from __future__ import annotations

from pathlib import Path

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config import load_config

from .bot import LightClawBot
from .logging_setup import log
from .personality import personality_search_paths, resolve_runtime_path


def main():
    """Start the LightClaw Telegram bot."""
    config = load_config()

    # Resolve runtime paths relative to LIGHTCLAW_HOME (if set) or project root.
    config.workspace_path = str(resolve_runtime_path(config.workspace_path))
    config.memory_db_path = str(resolve_runtime_path(config.memory_db_path))
    config.skills_state_path = str(resolve_runtime_path(config.skills_state_path))

    # Ensure workspace directory exists
    workspace = Path(config.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    # Validate required config
    if not config.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN is required. Set it in .env")
        return

    if not config.llm_provider:
        log.error(
            "No LLM provider configured. Set LLM_PROVIDER and the corresponding API key in .env"
        )
        return

    log.info("🦞 LightClaw starting...")
    log.info(f"   Provider: {config.llm_provider} ({config.llm_model})")
    log.info(f"   Memory DB: {config.memory_db_path}")
    log.info(f"   Workspace: {config.workspace_path}")
    log.info(f"   Skills state: {config.skills_state_path}")
    log.info(f"   Skills hub: {config.skills_hub_base_url}")
    log.info(f"   Context window: {config.context_window:,} tokens")
    log.info(f"   Max output: {config.max_output_tokens:,} tokens")
    log.info(f"   Local agent timeout: {config.local_agent_timeout_sec}s")
    log.info(
        f"   Delegation safety: {config.local_agent_safety_mode} "
        f"({len(config.local_agent_deny_patterns)} custom pattern(s))"
    )
    if config.groq_api_key:
        log.info("   Voice: ✅ Groq Whisper enabled")
    else:
        log.info("   Voice: ❌ disabled (set GROQ_API_KEY)")
    if config.telegram_allowed_users:
        log.info(f"   Allowed users: {', '.join(config.telegram_allowed_users)}")
    else:
        log.info("   Allowed users: everyone")

    bot = LightClawBot(config)

    # Print memory stats
    stats = bot.memory.stats()
    skill_count = len(bot.skills.list_skills())
    log.info(
        f"   Memory: {stats['total_interactions']} interactions, "
        f"{stats['unique_sessions']} sessions, "
        f"{stats['vocabulary_size']} vocabulary terms"
    )
    log.info(f"   Skills: {skill_count} installed")

    # Print personality source
    search_paths = personality_search_paths(config.workspace_path)
    loaded = []
    for filename in ["IDENTITY.md", "SOUL.md", "USER.md"]:
        if any((base / filename).exists() for base in search_paths):
            loaded.append(filename)
    if loaded:
        primary = search_paths[0]
        log.info(f"   Personality: {', '.join(loaded)} ({primary})")
    else:
        log.info("   Personality: built-in default")

    async def _post_init(application: Application):
        await bot._ensure_cron_task(application.bot)

    # Build Telegram application
    app = Application.builder().token(config.telegram_bot_token).post_init(_post_init).build()

    # Register handlers
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("clear", bot.cmd_clear))
    app.add_handler(CommandHandler("wipe_memory", bot.cmd_wipe_memory))
    app.add_handler(CommandHandler("wipe", bot.cmd_wipe_memory))
    app.add_handler(CommandHandler("memory", bot.cmd_memory))
    app.add_handler(CommandHandler("recall", bot.cmd_recall))
    app.add_handler(CommandHandler("skills", bot.cmd_skills))
    app.add_handler(CommandHandler("agent", bot.cmd_agent))
    app.add_handler(CommandHandler("heartbeat", bot.cmd_heartbeat))
    app.add_handler(CommandHandler("cron", bot.cmd_cron))
    app.add_handler(CommandHandler("show", bot.cmd_show))
    app.add_handler(MessageHandler(filters.VOICE, bot.handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
    )
    app.add_error_handler(bot.on_error)

    log.info("🦞 LightClaw is running! Press Ctrl+C to stop.")

    # Start polling
    # Longer Telegram long-poll timeout reduces idle request churn.
    app.run_polling(drop_pending_updates=True, timeout=30)

if __name__ == "__main__":
    main()
