#!/usr/bin/env bash
# LightClaw isolated installer.
# Local checkout: bash setup.sh
# Remote bootstrap: curl -fsSL https://raw.githubusercontent.com/OthmaneBlial/lightclaw/main/setup.sh | bash

set -euo pipefail

REPOSITORY_URL="https://github.com/OthmaneBlial/lightclaw.git"
INSTALL_ROOT="${LIGHTCLAW_INSTALL_ROOT:-${HOME}/.local/share/lightclaw}"
SOURCE_DIR="${INSTALL_ROOT}/source"
VENV_DIR="${INSTALL_ROOT}/venv"
BIN_DIR="${HOME}/.local/bin"
COMMAND_PATH="${BIN_DIR}/lightclaw"
OWNERSHIP_MARKER="${INSTALL_ROOT}/.lightclaw-install"

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'LightClaw install error: %s\n' "$*" >&2
    exit 1
}

command -v python3 >/dev/null 2>&1 || fail "Python 3.10+ is required."

python3 - <<'PY' || fail "Python 3.10+ is required."
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
PY

SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
LOCAL_SOURCE=""
if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
    SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$SCRIPT_SOURCE")" && pwd -P)"
    if [ -f "${SCRIPT_DIR}/pyproject.toml" ]; then
        LOCAL_SOURCE="$SCRIPT_DIR"
    fi
fi

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
printf 'managed-by=lightclaw\n' > "$OWNERSHIP_MARKER"

if [ -n "$LOCAL_SOURCE" ]; then
    PACKAGE_SOURCE="$LOCAL_SOURCE"
    say "Installing from local checkout: $PACKAGE_SOURCE"
else
    command -v git >/dev/null 2>&1 || fail "git is required for remote bootstrap."
    if [ -d "${SOURCE_DIR}/.git" ]; then
        say "Updating managed source checkout..."
        git -C "$SOURCE_DIR" pull --ff-only origin main
    elif [ -e "$SOURCE_DIR" ]; then
        fail "$SOURCE_DIR exists but is not a LightClaw Git checkout. Move it and retry."
    else
        say "Cloning LightClaw into the app data directory..."
        git clone --depth 1 --branch main "$REPOSITORY_URL" "$SOURCE_DIR"
    fi
    PACKAGE_SOURCE="$SOURCE_DIR"
fi

if [ ! -x "${VENV_DIR}/bin/python" ]; then
    say "Creating isolated environment: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

say "Installing LightClaw and all provider adapters into the isolated environment..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --upgrade "${PACKAGE_SOURCE}[providers]"

if [ -e "$COMMAND_PATH" ] || [ -L "$COMMAND_PATH" ]; then
    current_target="$(readlink "$COMMAND_PATH" 2>/dev/null || true)"
    if [ "$current_target" != "${VENV_DIR}/bin/lightclaw" ]; then
        backup_path="${COMMAND_PATH}.bak-$(date '+%Y%m%d-%H%M%S')"
        mv "$COMMAND_PATH" "$backup_path"
        say "Existing command preserved at: $backup_path"
    fi
fi
ln -sfn "${VENV_DIR}/bin/lightclaw" "$COMMAND_PATH"

say ""
say "LightClaw installed safely."
say "  Command: $COMMAND_PATH"
say "  Environment: $VENV_DIR"
say "  Config: ${HOME}/.config/lightclaw/config.env"
say "  Runtime data: ${HOME}/.lightclaw"
say ""

if [ "${LIGHTCLAW_SKIP_ONBOARD:-no}" = "yes" ]; then
    say "Onboarding skipped. Run: $COMMAND_PATH onboard"
    exit 0
fi

exec "$COMMAND_PATH" onboard --configure
