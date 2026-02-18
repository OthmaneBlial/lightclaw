"""
LightClaw — Configuration
Flat .env-based configuration system.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


LATEST_MODEL_DEFAULTS = {
    "openai": "gpt-5.2",
    "xai": "grok-4-latest",
    "claude": "claude-opus-4-5",
    "gemini": "gemini-3-flash-preview",
    "zai": "glm-5",
}

_MODEL_DEFAULT_SENTINELS = {"", "latest", "auto", "default"}


@dataclass
class Config:
    # LLM Provider
    llm_provider: str = ""
    llm_model: str = ""

    # API Keys
    openai_api_key: str = ""
    xai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    zai_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: list[str] = field(default_factory=list)

    # Memory
    memory_db_path: str = ".lightclaw/lightclaw.db"
    memory_top_k: int = 5

    # Workspace & Context
    workspace_path: str = ".lightclaw/workspace"
    context_window: int = 128000
    max_output_tokens: int = 12000

    # Skills
    skills_hub_base_url: str = "https://clawhub.ai"
    skills_state_path: str = ".lightclaw/skills_state.json"

    # Optional: Groq API key for voice transcription
    groq_api_key: str = ""


def _resolve_model(provider: str, model: str) -> str:
    """Resolve empty/default model values to provider-specific latest defaults."""
    provider_name = (provider or "").strip().lower()
    requested = (model or "").strip()
    if requested.lower() in _MODEL_DEFAULT_SENTINELS:
        return LATEST_MODEL_DEFAULTS.get(provider_name, LATEST_MODEL_DEFAULTS["openai"])
    return requested


def load_config() -> Config:
    """Load config from environment variables with auto-detection."""
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    allowed = [u.strip() for u in allowed_raw.split(",") if u.strip()] if allowed_raw else []

    cfg = Config(
        llm_provider=os.getenv("LLM_PROVIDER", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        xai_api_key=os.getenv("XAI_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        zai_api_key=os.getenv("ZAI_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_users=allowed,
        memory_db_path=os.getenv("MEMORY_DB_PATH", ".lightclaw/lightclaw.db"),
        memory_top_k=int(os.getenv("MEMORY_TOP_K", "5")),
        workspace_path=os.getenv("WORKSPACE_PATH", ".lightclaw/workspace"),
        context_window=int(os.getenv("CONTEXT_WINDOW", "128000")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "12000")),
        skills_hub_base_url=os.getenv("SKILLS_HUB_BASE_URL", "https://clawhub.ai") or "https://clawhub.ai",
        skills_state_path=os.getenv("SKILLS_STATE_PATH", ".lightclaw/skills_state.json") or ".lightclaw/skills_state.json",
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
    )

    # Auto-detect provider from API keys if not explicitly set
    if not cfg.llm_provider:
        if cfg.openai_api_key:
            cfg.llm_provider = "openai"
        elif cfg.xai_api_key:
            cfg.llm_provider = "xai"
        elif cfg.anthropic_api_key:
            cfg.llm_provider = "claude"
        elif cfg.gemini_api_key:
            cfg.llm_provider = "gemini"
        elif cfg.zai_api_key:
            cfg.llm_provider = "zai"

    cfg.llm_provider = cfg.llm_provider.strip().lower()
    cfg.llm_model = _resolve_model(cfg.llm_provider, cfg.llm_model)
    cfg.max_output_tokens = max(512, int(cfg.max_output_tokens))

    return cfg
