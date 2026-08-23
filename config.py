"""
LightClaw — Configuration
Flat .env-based configuration system.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

LATEST_MODEL_DEFAULTS = {
    "openai": "gpt-5.2",
    "xai": "grok-4-latest",
    "claude": "claude-opus-4-5",
    "gemini": "gemini-3-flash-preview",
    "deepseek": "deepseek-chat",
    "zai": "glm-5",
}

_MODEL_DEFAULT_SENTINELS = {"", "latest", "auto", "default"}
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
CAPABILITY_PROFILES = {"observe", "workspace-write", "trusted-command"}


def _config_file_candidates() -> list[Path]:
    """Return explicit, app-specific, then legacy config candidates."""
    override = os.getenv("LIGHTCLAW_CONFIG", "").strip()
    if override:
        return [Path(override).expanduser().resolve()]

    home_raw = os.getenv("LIGHTCLAW_HOME", "").strip()
    home = Path(home_raw).expanduser().resolve() if home_raw else Path.home().resolve()
    xdg_raw = os.getenv("XDG_CONFIG_HOME", "").strip()
    config_root = (
        Path(xdg_raw).expanduser().resolve()
        if xdg_raw and not home_raw
        else home / ".config"
    )
    return [config_root / "lightclaw" / "config.env", home / ".env"]


def _load_config_env() -> str:
    """Load one known config file without directory crawling."""
    for candidate in _config_file_candidates():
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate.as_posix()
    return _config_file_candidates()[0].as_posix()


def _strip_inline_comment(value: str) -> str:
    """Strip shell-style inline comments for unquoted env values."""
    if not value:
        return ""
    cleaned = value.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("#"):
        return ""
    return re.sub(r"\s+#.*$", "", cleaned).strip()


def _parse_allowed_users(raw: str) -> list[str]:
    """Parse TELEGRAM_ALLOWED_USERS as comma-separated numeric user IDs."""
    cleaned = _strip_inline_comment(raw)
    if not cleaned:
        return []

    users: list[str] = []
    for chunk in cleaned.split(","):
        token = chunk.strip()
        if not token:
            continue
        if token.startswith("#"):
            break
        token = token.split("#", 1)[0].strip()
        if not token:
            continue
        # Telegram user IDs are numeric; ignore placeholder/comment text safely.
        if token.lstrip("-").isdigit():
            users.append(token)
    return users


def _parse_deny_patterns(raw: str) -> list[str]:
    """Parse LOCAL_AGENT_DENY_PATTERNS into a list of regex pattern strings."""
    if not raw:
        return []
    patterns: list[str] = []
    for chunk in re.split(r"[,\n;]+", raw):
        token = _strip_inline_comment(chunk)
        if token:
            patterns.append(token)
    return patterns


def _parse_bool(raw: str, default: bool = False) -> bool:
    cleaned = _strip_inline_comment(raw or "")
    if not cleaned:
        return default
    return cleaned.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_multi_default_agents(raw: str) -> list[str]:
    alias_map = {
        "codex": "codex",
        "codex-cli": "codex",
        "claude": "claude",
        "claude-code": "claude",
    }

    cleaned = _strip_inline_comment(raw or "")
    if not cleaned:
        return ["claude", "codex"]

    agents: list[str] = []
    for chunk in re.split(r"[,\s;]+", cleaned):
        token = chunk.strip().lower()
        if not token:
            continue
        canonical = alias_map.get(token)
        if not canonical:
            continue
        if canonical not in agents:
            agents.append(canonical)

    return agents or ["claude", "codex"]


@dataclass
class Config:
    config_path: str = ""

    # LLM Provider
    llm_provider: str = ""
    llm_model: str = ""

    # Provider credentials
    openai_api_key: str = ""
    xai_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_auth_token: str = ""
    anthropic_base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    gemini_api_key: str = ""
    deepseek_api_key: str = ""
    zai_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: list[str] = field(default_factory=list)
    telegram_public_bot_ack: bool = False

    # Memory
    memory_db_path: str = ".lightclaw/lightclaw.db"
    memory_top_k: int = 5
    memory_retention_days: int = 90
    memory_max_interactions: int = 10_000
    memory_max_db_mb: int = 64
    memory_query_timeout_ms: int = 100
    memory_candidate_limit: int = 200

    # Workspace & Context
    workspace_path: str = ".lightclaw/workspace"
    context_window: int = 128000
    max_output_tokens: int = 12000
    local_agent_timeout_sec: int = 1800
    local_agent_progress_interval_sec: int = 30
    local_agent_safety_mode: str = "strict"
    local_agent_capability_profile: str = "workspace-write"
    local_agent_deny_patterns: list[str] = field(default_factory=list)
    local_agent_multi_default_agents: list[str] = field(
        default_factory=lambda: ["claude", "codex"]
    )
    local_agent_multi_auto_continue: bool = False
    local_agent_multi_repair_attempts: int = 1

    # Skills
    skills_hub_base_url: str = "https://clawhub.ai"
    skills_state_path: str = ".lightclaw/skills_state.json"

    # Optional: Groq API key for voice transcription
    groq_api_key: str = ""


def _resolve_model(provider: str, model: str) -> str:
    """Resolve empty/default model values to provider-specific latest defaults."""
    provider_name = _strip_inline_comment(provider or "").lower()
    requested = _strip_inline_comment(model or "")
    if requested.lower() in _MODEL_DEFAULT_SENTINELS:
        return LATEST_MODEL_DEFAULTS.get(provider_name, LATEST_MODEL_DEFAULTS["openai"])
    return requested


def load_config() -> Config:
    """Load config from environment variables with auto-detection."""
    loaded_config_path = _load_config_env()
    allowed_raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    allowed = _parse_allowed_users(allowed_raw)

    cfg = Config(
        config_path=loaded_config_path,
        llm_provider=_strip_inline_comment(os.getenv("LLM_PROVIDER", "")),
        llm_model=_strip_inline_comment(os.getenv("LLM_MODEL", "")),
        openai_api_key=_strip_inline_comment(os.getenv("OPENAI_API_KEY", "")),
        xai_api_key=_strip_inline_comment(os.getenv("XAI_API_KEY", "")),
        anthropic_api_key=_strip_inline_comment(os.getenv("ANTHROPIC_API_KEY", "")),
        anthropic_auth_token=_strip_inline_comment(os.getenv("ANTHROPIC_AUTH_TOKEN", "")),
        anthropic_base_url=_strip_inline_comment(
            os.getenv("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL)
        )
        or DEFAULT_ANTHROPIC_BASE_URL,
        gemini_api_key=_strip_inline_comment(os.getenv("GEMINI_API_KEY", "")),
        deepseek_api_key=_strip_inline_comment(os.getenv("DEEPSEEK_API_KEY", "")),
        zai_api_key=_strip_inline_comment(os.getenv("ZAI_API_KEY", "")),
        telegram_bot_token=_strip_inline_comment(os.getenv("TELEGRAM_BOT_TOKEN", "")),
        telegram_allowed_users=allowed,
        telegram_public_bot_ack=_parse_bool(
            os.getenv("LIGHTCLAW_PUBLIC_BOT_ACK", "no"),
            default=False,
        ),
        memory_db_path=os.getenv("MEMORY_DB_PATH", ".lightclaw/lightclaw.db"),
        memory_top_k=int(os.getenv("MEMORY_TOP_K", "5")),
        memory_retention_days=int(os.getenv("MEMORY_RETENTION_DAYS", "90")),
        memory_max_interactions=int(os.getenv("MEMORY_MAX_INTERACTIONS", "10000")),
        memory_max_db_mb=int(os.getenv("MEMORY_MAX_DB_MB", "64")),
        memory_query_timeout_ms=int(os.getenv("MEMORY_QUERY_TIMEOUT_MS", "100")),
        memory_candidate_limit=int(os.getenv("MEMORY_CANDIDATE_LIMIT", "200")),
        workspace_path=os.getenv("WORKSPACE_PATH", ".lightclaw/workspace"),
        context_window=int(os.getenv("CONTEXT_WINDOW", "128000")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "12000")),
        local_agent_timeout_sec=int(os.getenv("LOCAL_AGENT_TIMEOUT_SEC", "1800")),
        local_agent_progress_interval_sec=int(
            os.getenv("LOCAL_AGENT_PROGRESS_INTERVAL_SEC", "30")
        ),
        local_agent_safety_mode=os.getenv("LOCAL_AGENT_SAFETY_MODE", "strict"),
        local_agent_capability_profile=os.getenv(
            "LOCAL_AGENT_CAPABILITY_PROFILE", "workspace-write"
        ),
        local_agent_deny_patterns=_parse_deny_patterns(
            os.getenv("LOCAL_AGENT_DENY_PATTERNS", "")
        ),
        local_agent_multi_default_agents=_parse_multi_default_agents(
            os.getenv("LOCAL_AGENT_MULTI_DEFAULT_AGENTS", "claude,codex")
        ),
        local_agent_multi_auto_continue=_parse_bool(
            os.getenv("LOCAL_AGENT_MULTI_AUTO_CONTINUE", "no"),
            default=False,
        ),
        local_agent_multi_repair_attempts=int(
            os.getenv("LOCAL_AGENT_MULTI_REPAIR_ATTEMPTS", "1")
        ),
        skills_hub_base_url=os.getenv("SKILLS_HUB_BASE_URL", "https://clawhub.ai") or "https://clawhub.ai",
        skills_state_path=os.getenv("SKILLS_STATE_PATH", ".lightclaw/skills_state.json") or ".lightclaw/skills_state.json",
        groq_api_key=_strip_inline_comment(os.getenv("GROQ_API_KEY", "")),
    )

    # Auto-detect provider from configured credentials if not explicitly set
    if not cfg.llm_provider:
        if cfg.openai_api_key:
            cfg.llm_provider = "openai"
        elif cfg.xai_api_key:
            cfg.llm_provider = "xai"
        elif cfg.anthropic_api_key or cfg.anthropic_auth_token:
            cfg.llm_provider = "claude"
        elif cfg.gemini_api_key:
            cfg.llm_provider = "gemini"
        elif cfg.deepseek_api_key:
            cfg.llm_provider = "deepseek"
        elif cfg.zai_api_key:
            cfg.llm_provider = "zai"

    cfg.llm_provider = cfg.llm_provider.strip().lower()
    cfg.llm_model = _resolve_model(cfg.llm_provider, cfg.llm_model)
    cfg.max_output_tokens = max(512, int(cfg.max_output_tokens))
    cfg.memory_top_k = max(1, min(50, int(cfg.memory_top_k)))
    cfg.memory_retention_days = max(1, min(3_650, int(cfg.memory_retention_days)))
    cfg.memory_max_interactions = max(100, min(1_000_000, int(cfg.memory_max_interactions)))
    cfg.memory_max_db_mb = max(1, min(4_096, int(cfg.memory_max_db_mb)))
    cfg.memory_query_timeout_ms = max(10, min(5_000, int(cfg.memory_query_timeout_ms)))
    cfg.memory_candidate_limit = max(10, min(2_000, int(cfg.memory_candidate_limit)))
    cfg.local_agent_timeout_sec = max(60, int(cfg.local_agent_timeout_sec))
    cfg.local_agent_progress_interval_sec = max(
        10, int(cfg.local_agent_progress_interval_sec)
    )
    cfg.local_agent_safety_mode = _strip_inline_comment(
        cfg.local_agent_safety_mode or "strict"
    ).lower()
    if cfg.local_agent_safety_mode not in {"off", "strict"}:
        cfg.local_agent_safety_mode = "strict"
    cfg.local_agent_capability_profile = _strip_inline_comment(
        cfg.local_agent_capability_profile or "workspace-write"
    ).lower()
    if cfg.local_agent_capability_profile not in CAPABILITY_PROFILES:
        cfg.local_agent_capability_profile = "workspace-write"
    if not cfg.local_agent_multi_default_agents:
        cfg.local_agent_multi_default_agents = ["claude", "codex"]
    cfg.local_agent_multi_repair_attempts = max(
        0,
        min(2, int(cfg.local_agent_multi_repair_attempts)),
    )

    return cfg
