#!/usr/bin/env python3
"""
Smoke-test configured LLM providers with a minimal prompt.

Usage:
  python scripts/provider_smoke_test.py
  python scripts/provider_smoke_test.py --providers openai,claude
  python scripts/provider_smoke_test.py --model claude-opus-4-5
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config, LATEST_MODEL_DEFAULTS, load_config
from providers import LLMClient

PROVIDER_KEY_ATTRS = {
    "openai": "openai_api_key",
    "xai": "xai_api_key",
    "claude": "anthropic_api_key",
    "gemini": "gemini_api_key",
    "deepseek": "deepseek_api_key",
    "zai": "zai_api_key",
}


def _build_provider_config(source_cfg: Config, provider: str, model: str) -> Config:
    """Clone config while forcing provider/model for one smoke test."""
    return Config(
        llm_provider=provider,
        llm_model=model,
        openai_api_key=source_cfg.openai_api_key,
        xai_api_key=source_cfg.xai_api_key,
        anthropic_api_key=source_cfg.anthropic_api_key,
        gemini_api_key=source_cfg.gemini_api_key,
        deepseek_api_key=source_cfg.deepseek_api_key,
        zai_api_key=source_cfg.zai_api_key,
        telegram_bot_token=source_cfg.telegram_bot_token,
        telegram_allowed_users=source_cfg.telegram_allowed_users,
        memory_db_path=source_cfg.memory_db_path,
        memory_top_k=source_cfg.memory_top_k,
        workspace_path=source_cfg.workspace_path,
        context_window=source_cfg.context_window,
        groq_api_key=source_cfg.groq_api_key,
    )


async def _check_provider(
    root_cfg: Config,
    provider: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
) -> Tuple[str, str]:
    key_attr = PROVIDER_KEY_ATTRS[provider]
    if not getattr(root_cfg, key_attr):
        return "SKIP", f"missing {key_attr}"

    cfg = _build_provider_config(root_cfg, provider, model)
    client = LLMClient(cfg)
    try:
        response = await asyncio.wait_for(
            client.chat([{"role": "user", "content": prompt}]),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return "FAIL", str(exc)

    text = (response or "").strip().replace("\n", " ")
    if not text:
        return "FAIL", "empty response"
    if text.lower().startswith("⚠️ error communicating with") or text.lower().startswith("error communicating with"):
        return "FAIL", text[:160]
    return "OK", text[:120]


async def _run(args: argparse.Namespace) -> int:
    root_cfg = load_config()
    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]

    invalid = [p for p in providers if p not in PROVIDER_KEY_ATTRS]
    if invalid:
        print(f"Invalid providers: {', '.join(invalid)}")
        print(f"Valid providers: {', '.join(PROVIDER_KEY_ATTRS.keys())}")
        return 2

    failures = 0
    for provider in providers:
        model = args.model or LATEST_MODEL_DEFAULTS[provider]
        status, detail = await _check_provider(
            root_cfg=root_cfg,
            provider=provider,
            model=model,
            prompt=args.prompt,
            timeout_seconds=args.timeout,
        )
        print(f"[{status}] {provider} ({model}) -> {detail}")
        if status == "FAIL":
            failures += 1

    if failures:
        print(f"\nDone with {failures} failure(s).")
        return 1

    print("\nDone with no hard failures.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test configured LLM providers.")
    parser.add_argument(
        "--providers",
        default="openai,xai,claude,gemini,deepseek,zai",
        help="Comma-separated providers to test.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Optional explicit model ID to use for all selected providers.",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: OK",
        help="Test prompt sent to each provider.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Timeout in seconds per provider.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
