#!/usr/bin/env bash
# Quick start — skips onboarding, uses existing .env

echo "🦞 Starting LightClaw..."
exec "$(dirname "$0")/lightclaw" run
