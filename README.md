# LightClaw

LightClaw is a **self-hosted Telegram AI agent** inspired by OpenClaw: a small Python codebase with long-term memory, multi-provider LLM routing, skills, and local multi-agent delegation.

If you are searching for an **OpenClaw alternative**, **OpenClaw in Python**, or a **Telegram AI bot with memory**, this project is built for that workflow.

<div align="center">
  <img src="logo.png" alt="LightClaw logo" width="420">
</div>

## Security Disclaimer

LightClaw can execute impactful actions (file edits and delegated local agent runs).  
Use least-privilege credentials, review installed skills, and restrict bot access with `TELEGRAM_ALLOWED_USERS`.

## Why LightClaw

- Lightweight and forkable: understand the core quickly and customize without framework overhead.
- Practical for solo builders: run on small VPS machines with minimal setup.
- Built for real usage: memory recall, file operations, skills, and delegated coding agents.

## Core Features

- Infinite memory with SQLite + semantic recall.
- 6 LLM providers: OpenAI, xAI, Anthropic, Gemini, DeepSeek, Z-AI.
- Telegram-first experience with command-driven workflow.
- Local terminal chat mode (`lightclaw chat`) using the same runtime stack.
- Skills system (hub + local skills).
- Local agent delegation (`codex`, `claude`) for large coding tasks.
- Smart multi-agent orchestration with auto-planning, dependencies, and confirmation flow.
- Workspace-native code generation/editing with compact delta reports.
- Optional voice transcription with Groq Whisper.

## Quick Start

### 1) One-command setup (recommended)

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git && cd lightclaw && bash setup.sh
```

`setup.sh` does everything automatically:

- Installs the `lightclaw` command at `~/.local/bin/lightclaw`
- Writes your config to `~/.env`
- Creates runtime files in `~/.lightclaw`

Then run:

```bash
lightclaw run
```

If your shell has not reloaded `PATH` yet, use:

```bash
~/.local/bin/lightclaw run
```

### 2) Manual setup

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git
cd lightclaw
pip install -r requirements.txt
./lightclaw onboard
```

Then edit `~/.env` and start:

```bash
./lightclaw run
```

## Minimal `.env` Example

```env
# Provider selection
LLM_PROVIDER=openai
LLM_MODEL=latest

# Provider keys (fill what you use)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
ANTHROPIC_AUTH_TOKEN=
ANTHROPIC_BASE_URL=
DEEPSEEK_API_KEY=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=

# Optional generation tuning
MAX_OUTPUT_TOKENS=12000

# Local delegated agents
LOCAL_AGENT_TIMEOUT_SEC=1800
LOCAL_AGENT_PROGRESS_INTERVAL_SEC=30
LOCAL_AGENT_MULTI_DEFAULT_AGENTS=claude,codex
LOCAL_AGENT_MULTI_AUTO_CONTINUE=no
LOCAL_AGENT_MULTI_REPAIR_ATTEMPTS=1
LOCAL_AGENT_SAFETY_MODE=off
LOCAL_AGENT_DENY_PATTERNS=

# Skills
SKILLS_HUB_BASE_URL=https://clawhub.ai
SKILLS_STATE_PATH=.lightclaw/skills_state.json
```

## CLI Commands

```bash
lightclaw onboard
lightclaw onboard --reset-env
lightclaw onboard --configure
lightclaw run
lightclaw run --provider deepseek --model deepseek-chat
lightclaw chat
```

## Telegram / Chat Commands

| Command | Purpose |
|---|---|
| `/help` | Show command help |
| `/memory` | Memory stats |
| `/recall <query>` | Semantic memory search |
| `/skills ...` | Search/install/activate skills |
| `/agent` | Local agent delegation controls |
| `/agent doctor` | Agent install/auth diagnostics |
| `/agent multi <goal>` | Auto-plan multi-agent run |
| `/agent multi @claude @codex <goal>` | Prefer specific agents |
| `/agent multi --agent backend=codex --agent qa=claude <goal>` | Explicit worker roster |
| `/agent multi confirm` | Execute pending plan |
| `/agent multi edit <feedback>` | Regenerate pending plan |
| `/agent multi cancel` | Cancel pending plan |
| `/show` | Current runtime/provider/model status |
| `/clear` | Reset current chat history |
| `/wipe_memory` | Wipe all saved memory (confirmation required) |

## Smart Multi-Agent Mode

Full guide with many usage examples: [MULTI_AGENT.md](MULTI_AGENT.md)

`/agent multi` supports three ways to define worker assignment:

1. Auto mode:

```text
/agent multi build a full stack todo app
```

2. Preferred agents (no labels):

```text
/agent multi @claude @codex build a full stack todo app
```

3. Explicit roster override (backward compatible):

```text
/agent multi --agent backend=codex --agent frontend=claude --agent docs=codex build a full stack todo app
```

How it runs:

- Plan is generated and shown first.
- Confirmation is required by default (`confirm`, `yes`) unless `LOCAL_AGENT_MULTI_AUTO_CONTINUE=yes`.
- `edit` lets you iterate the plan before execution.
- `cancel` or `no` clears the pending plan.
- Execution now follows true DAG scheduling, so downstream lanes can start as soon as their own dependencies finish.
- Each worker gets owned paths, must write `handoff/<lane>.md` plus `handoff/<lane>.json`, and is checked against lightweight acceptance rules.
- The same contract system now handles non-coding lanes too, including research, analysis, authoring, and review/validation roles.
- Acceptance can now run small bounded repo-local commands when a lane declares `command_succeeds`.
- Backend/frontend lanes also get automatic handoff JSON field checks, so `outputs.endpoints` and `outputs.api_calls` must actually be populated.
- Docs/authoring lanes now get the same treatment via `outputs.deliverables`, so non-code artifacts are tracked in a machine-readable way too.
- Research/review and docs/authoring runs now also get lightweight cross-lane findings/deliverables audits in the final report.
- Backend/frontend runs also get a lightweight cross-lane API audit from handoff JSON, so method/path mismatches are surfaced in the final report.
- Failed lanes can get a small self-repair pass controlled by `LOCAL_AGENT_MULTI_REPAIR_ATTEMPTS` (clamped to `0..2`).

## Supported Providers

| Provider | Set `LLM_PROVIDER` | Example Models |
|---|---|---|
| OpenAI | `openai` | `gpt-5.2`, `gpt-5.2-mini` |
| xAI | `xai` | `grok-4-latest` |
| Claude | `claude` | `claude-opus-4-5`, `claude-sonnet-4-5` |
| Gemini | `gemini` | `gemini-3-flash-preview`, `gemini-2.5-flash` |
| DeepSeek | `deepseek` | `deepseek-chat`, `deepseek-reasoner` |
| Z-AI | `zai` | `glm-5`, `glm-4.7` |

Quick provider check:

```bash
python scripts/provider_smoke_test.py
```

## Skills (Hub + Local)

Examples:

```text
/skills search sonos
/skills add sonoscli
/skills use sonoscli
/skills off sonoscli
/skills create my_custom_skill "My private workflow"
```

Paths:

- Hub skills: `~/.lightclaw/skills/hub/<slug>/SKILL.md`
- Local skills: `~/.lightclaw/skills/local/<name>/SKILL.md`

## Architecture (Short)

```text
Telegram or terminal chat
  -> memory recall (SQLite + semantic search)
  -> provider routing (OpenAI/xAI/Claude/Gemini/DeepSeek/Z-AI)
  -> response + optional file operations in ~/.lightclaw/workspace
  -> optional delegated local agents (single or multi-worker)
```

## OpenClaw and LightClaw

- OpenClaw: larger TypeScript platform for broad, multi-app orchestration.
- LightClaw: focused Python core for fast local customization and Telegram-first workflows.

OpenClaw links:

- https://github.com/openclaw/openclaw
- https://docs.openclaw.ai/

## Requirements

- Python 3.10+
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- API credentials for at least one supported LLM provider
- Optional: Groq API key for voice transcription

## License

MIT

---

LightClaw is intentionally small: easy to read, easy to fork, and fast to ship.
