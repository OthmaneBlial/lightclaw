#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# 🦞 LightClaw — One-Command Setup
# ──────────────────────────────────────────────────────────────
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/OthmaneBlial/lightclaw/master/setup.sh | bash
#   — or —
#   git clone https://github.com/OthmaneBlial/lightclaw.git && cd lightclaw && bash setup.sh
# ──────────────────────────────────────────────────────────────

set -e

# ── Colors & Formatting ──────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# ── Helper Functions ─────────────────────────────────────────

banner() {
    clear
    echo -e "${RED}"
    echo '    ___       __   __  _______              '
    echo '   / (_)___ _/ /_ / /_/ ____/ /__ __      __'
    echo '  / / / __ `/ __ \/ __/ /   / / _ `/ | /| / /'
    echo ' / / / /_/ / / / / /_/ /___/ / /_/ /| |/ |/ / '
    echo '/_/_/\__, /_/ /_/\__/\____/_/\__,_/ |__/|__/  '
    echo '    /____/                                    '
    echo -e "${NC}"
    echo -e "${DIM}  The Featherweight Core of OpenClaw${NC}"
    echo ""
}

step() {
    echo -e "\n${CYAN}▸${NC} ${BOLD}$1${NC}"
}

success() {
    echo -e "  ${GREEN}✅ $1${NC}"
}

warn() {
    echo -e "  ${YELLOW}⚠️  $1${NC}"
}

fail() {
    echo -e "  ${RED}❌ $1${NC}"
    exit 1
}

ask() {
    echo -en "  ${MAGENTA}?${NC} $1 "
}

# ── Preflight Checks ─────────────────────────────────────────

banner

step "Checking prerequisites..."

# Python 3.10+
if command -v python3 &>/dev/null; then
    PY=$(python3 --version | grep -oP '\d+\.\d+')
    PY_MAJOR=$(echo "$PY" | cut -d. -f1)
    PY_MINOR=$(echo "$PY" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        success "Python $PY found"
    else
        fail "Python 3.10+ required (found $PY). Install from https://python.org"
    fi
else
    fail "Python 3 not found. Install from https://python.org"
fi

# pip
if command -v pip3 &>/dev/null || python3 -m pip --version &>/dev/null 2>&1; then
    success "pip available"
else
    fail "pip not found. Install: python3 -m ensurepip --upgrade"
fi

# Git (optional — only needed if not already cloned)
if [ ! -f "main.py" ]; then
    if command -v git &>/dev/null; then
        success "git found"
    else
        fail "git not found (needed to clone the repo). Install git first."
    fi
fi

# ── Clone if needed ──────────────────────────────────────────

if [ ! -f "main.py" ]; then
    step "Cloning LightClaw..."
    git clone https://github.com/OthmaneBlial/lightclaw.git
    cd lightclaw
    success "Cloned into ./lightclaw"
fi

# ── Install Dependencies ─────────────────────────────────────

step "Installing Python dependencies..."
pip3 install -r requirements.txt -q 2>/dev/null || python3 -m pip install -r requirements.txt -q
success "Dependencies installed"

# ──────────────────────────────────────────────────────────────
# 🎯 Interactive Onboarding
# ──────────────────────────────────────────────────────────────

banner
echo -e "${BOLD}Welcome to LightClaw setup!${NC}"
echo -e "${DIM}Let's configure your AI assistant in under 2 minutes.${NC}\n"

# ── Step 1: Choose LLM Provider ──────────────────────────────

step "Choose your AI provider"
echo ""
echo -e "  ${BOLD}1)${NC} OpenAI      ${DIM}(ChatGPT — gpt-5.2)${NC}"
echo -e "  ${BOLD}2)${NC} xAI         ${DIM}(Grok — grok-4-latest)${NC}"
echo -e "  ${BOLD}3)${NC} Anthropic   ${DIM}(Claude — claude-opus-4-5)${NC}"
echo -e "  ${BOLD}4)${NC} Google      ${DIM}(Gemini — gemini-3-flash-preview)${NC}"
echo -e "  ${BOLD}5)${NC} DeepSeek    ${DIM}(deepseek-chat — DeepSeek-V3.2 alias)${NC}"
echo -e "  ${BOLD}6)${NC} Z-AI        ${DIM}(GLM — glm-5)${NC}"
echo ""

PROVIDER=""
PROVIDER_NAME=""
API_KEY_ENV=""
DEFAULT_MODEL=""
ANTHROPIC_BASE_URL=""

while [ -z "$PROVIDER" ]; do
    ask "Enter number [1-6]:"
    read -r choice
    case $choice in
        1) PROVIDER="openai";  PROVIDER_NAME="OpenAI";  API_KEY_ENV="OPENAI_API_KEY";   DEFAULT_MODEL="gpt-5.2" ;;
        2) PROVIDER="xai";     PROVIDER_NAME="xAI";     API_KEY_ENV="XAI_API_KEY";      DEFAULT_MODEL="grok-4-latest" ;;
        3) PROVIDER="claude";  PROVIDER_NAME="Anthropic"; API_KEY_ENV="ANTHROPIC_API_KEY"; DEFAULT_MODEL="claude-opus-4-5" ;;
        4) PROVIDER="gemini";  PROVIDER_NAME="Google";   API_KEY_ENV="GEMINI_API_KEY";   DEFAULT_MODEL="gemini-3-flash-preview" ;;
        5) PROVIDER="deepseek"; PROVIDER_NAME="DeepSeek"; API_KEY_ENV="DEEPSEEK_API_KEY"; DEFAULT_MODEL="deepseek-chat" ;;
        6) PROVIDER="zai";     PROVIDER_NAME="Z-AI";     API_KEY_ENV="ZAI_API_KEY";      DEFAULT_MODEL="glm-5" ;;
        *) echo -e "  ${RED}Invalid choice. Enter 1-6.${NC}" ;;
    esac
done
success "Selected: $PROVIDER_NAME ($DEFAULT_MODEL)"

# ── Step 2: Provider Credentials ─────────────────────────────

step "Enter your $PROVIDER_NAME credentials"
echo -e "  ${DIM}Get them from your provider's dashboard${NC}"

API_KEY=""
if [ "$PROVIDER" = "claude" ]; then
    echo ""
    echo -e "  ${BOLD}Choose Claude auth mode:${NC}"
    echo -e "  ${BOLD}1)${NC} API key (${DIM}ANTHROPIC_API_KEY${NC})"
    echo -e "  ${BOLD}2)${NC} Auth token / subscription (${DIM}ANTHROPIC_AUTH_TOKEN${NC})"
    echo ""

    CLAUDE_AUTH_MODE=""
    while [ -z "$CLAUDE_AUTH_MODE" ]; do
        ask "Enter number [1-2]:"
        read -r auth_choice
        case $auth_choice in
            1) CLAUDE_AUTH_MODE="api_key"; API_KEY_ENV="ANTHROPIC_API_KEY" ;;
            2) CLAUDE_AUTH_MODE="auth_token"; API_KEY_ENV="ANTHROPIC_AUTH_TOKEN" ;;
            *) echo -e "  ${RED}Invalid choice. Enter 1-2.${NC}" ;;
        esac
    done
fi

while [ -z "$API_KEY" ]; do
    ask "$API_KEY_ENV:"
    read -rs API_KEY  # -s hides input (it's a secret)
    echo ""
    if [ -z "$API_KEY" ]; then
        echo -e "  ${RED}Credential cannot be empty.${NC}"
    fi
done
success "Credentials saved (hidden)"

if [ "$PROVIDER" = "claude" ]; then
    ask "Anthropic base URL override (optional, Enter = default https://api.anthropic.com):"
    read -r ANTHROPIC_BASE_URL
fi

# ── Step 3: Custom model (optional) ──────────────────────────

step "Choose model"
ask "Model name [${DEFAULT_MODEL}]:"
read -r CUSTOM_MODEL
MODEL="${CUSTOM_MODEL:-$DEFAULT_MODEL}"
success "Using model: $MODEL"

# ── Step 4: Telegram Bot Setup ───────────────────────────────

step "Set up Telegram Bot"
echo ""
echo -e "  ${BOLD}How to get a Telegram bot token:${NC}"
echo ""
echo -e "  1. Open Telegram and search for ${CYAN}@BotFather${NC}"
echo -e "  2. Send ${CYAN}/newbot${NC}"
echo -e "  3. Choose a ${BOLD}name${NC} (e.g. \"My LightClaw\")"
echo -e "  4. Choose a ${BOLD}username${NC} (e.g. \"my_lightclaw_bot\")"
echo -e "  5. BotFather will give you a token like:"
echo -e "     ${DIM}123456789:ABCdefGHIjklMNOpqrSTUvwxYZ${NC}"
echo -e "  6. Copy that token and paste it below"
echo ""

BOT_TOKEN=""
while [ -z "$BOT_TOKEN" ]; do
    ask "Telegram Bot Token:"
    read -r BOT_TOKEN
    if [ -z "$BOT_TOKEN" ]; then
        echo -e "  ${RED}Token cannot be empty.${NC}"
    elif [[ ! "$BOT_TOKEN" =~ ^[0-9]+:.+ ]]; then
        warn "Token doesn't look right (expected format: 123456:ABC...). Continuing anyway."
    fi
done
success "Telegram bot token saved"

# ── Step 5: Allowed Users (optional) ─────────────────────────

step "Restrict access (optional)"
echo -e "  ${DIM}Leave blank to allow everyone, or enter Telegram user IDs (comma-separated).${NC}"
echo -e "  ${DIM}Get your ID from @userinfobot on Telegram.${NC}"

ask "Allowed user IDs []:"
read -r ALLOWED_USERS

if [ -n "$ALLOWED_USERS" ]; then
    success "Restricted to: $ALLOWED_USERS"
else
    success "Access: open to everyone"
fi

# ── Step 6: Voice transcription (optional) ───────────────────

step "Voice message support (optional)"
echo -e "  ${DIM}Requires a Groq API key for Whisper transcription.${NC}"
echo -e "  ${DIM}Get one free at https://console.groq.com${NC}"

ask "Groq API key (press Enter to skip):"
read -rs GROQ_KEY
echo ""

if [ -n "$GROQ_KEY" ]; then
    success "Voice transcription enabled"
else
    success "Voice transcription skipped (can add later in .env)"
fi

# ── Generate .env ─────────────────────────────────────────────

step "Creating .env configuration..."

cat > .env << EOF
# ── LightClaw Configuration ──────────────
# Generated by setup.sh on $(date '+%Y-%m-%d %H:%M')

# LLM Provider
LLM_PROVIDER=$PROVIDER
LLM_MODEL=$MODEL
$API_KEY_ENV=$API_KEY
ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL

# Telegram
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_ALLOWED_USERS=$ALLOWED_USERS

# Memory
MEMORY_DB_PATH=.lightclaw/lightclaw.db
MEMORY_TOP_K=5

# Workspace & Context
WORKSPACE_PATH=.lightclaw/workspace
CONTEXT_WINDOW=128000

# Voice (optional)
GROQ_API_KEY=$GROQ_KEY
EOF

success ".env file created"

# ── Final Summary ─────────────────────────────────────────────

banner
echo -e "${GREEN}${BOLD}🎉 Setup complete!${NC}\n"
echo -e "  ${BOLD}Provider:${NC}  $PROVIDER_NAME"
echo -e "  ${BOLD}Model:${NC}    $MODEL"
echo -e "  ${BOLD}Telegram:${NC} configured ✅"
if [ -n "$GROQ_KEY" ]; then
    echo -e "  ${BOLD}Voice:${NC}    enabled ✅"
else
    echo -e "  ${BOLD}Voice:${NC}    disabled (add GROQ_API_KEY to .env later)"
fi
if [ -n "$ALLOWED_USERS" ]; then
    echo -e "  ${BOLD}Access:${NC}   restricted to $ALLOWED_USERS"
else
    echo -e "  ${BOLD}Access:${NC}   open to everyone"
fi
echo ""
echo -e "${DIM}  Config saved to .env — edit anytime.${NC}"
echo -e "${DIM}  Personality files in .lightclaw/workspace/ — customize your bot's character.${NC}"
echo ""

# ── Start the bot ─────────────────────────────────────────────

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
ask "Start LightClaw now? [Y/n]:"
read -r START_NOW

if [[ "$START_NOW" =~ ^[Nn] ]]; then
    echo ""
    echo -e "  ${BOLD}To start later, run:${NC}"
    echo -e "  ${CYAN}./lightclaw run${NC}"
    echo ""
    echo -e "  ${BOLD}🦞 See you soon!${NC}"
    exit 0
fi

echo ""
echo -e "${GREEN}${BOLD}🦞 Starting LightClaw...${NC}"
echo -e "${DIM}   Press Ctrl+C to stop${NC}"
echo ""

exec ./lightclaw run
