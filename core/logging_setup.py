"""Logging configuration for LightClaw."""

from __future__ import annotations

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lightclaw")

# Reduce noisy transport logs by default (can be re-enabled with LIGHTCLAW_VERBOSE_HTTP=1).
if os.getenv("LIGHTCLAW_VERBOSE_HTTP", "").strip().lower() not in {"1", "true", "yes"}:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)
