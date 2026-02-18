<div align="center">

  <h1>🦞 LightClaw</h1>

  <h3>The Featherweight Core of OpenClaw — Your AI Agent in a Tiny Codebase</h3>

  <p><strong>OpenClaw-inspired Python AI agent</strong> for Telegram, long-term memory, and multi-provider LLM support.</p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/Core-lean-brightgreen" alt="Core">
    <img src="https://img.shields.io/badge/Repo-~5k_lines-blue" alt="Lines">
    <img src="https://img.shields.io/badge/LLM_Providers-5-purple" alt="Providers">
    <img src="https://img.shields.io/badge/RAM-<50MB-orange" alt="RAM">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>

  <p><i>Fork it. Hack it. Ship it. No framework tax.</i></p>

</div>

---

## Why LightClaw Exists

**OpenClaw** is a powerful, full-featured AI agent platform — but it's also *big*. Dozens of packages, multiple channels, tool registries, message buses, plugin systems. It's built for scale and enterprise use.

**LightClaw** is the opposite. It's the *distilled essence* of the OpenClaw idea, stripped down to the atomic minimum:

If you are searching for an **OpenClaw alternative**, **OpenClaw in Python**, or a **self-hosted Telegram AI assistant with memory**, this repository is built for that exact use case.

```
OpenClaw:     50+ packages │ 20k+ lines  │ TypeScript │ 10+ channels │ 12+ providers │ >1GB RAM
LightClaw:    5 files       │ ~1300 lines │ Python │ Telegram only │ 6 providers │ <50MB RAM
```

Think of LightClaw as **the starter engine** — the part of a rocket that ignites first. It contains the core DNA of OpenClaw (LLM routing, memory, conversational agent) but removes everything else. No message bus. No plugin registry. No tool orchestration. Just a direct pipeline:

```
📱 Telegram Message → 🧠 Memory Recall → 🤖 LLM → 💡 HTML Format → 💬 Reply
```

## Looking for OpenClaw?

- OpenClaw GitHub: https://github.com/openclaw/openclaw
- OpenClaw docs: https://docs.openclaw.ai/
- LightClaw focuses on the lightweight Python path: Telegram-first, memory-enabled, and easy to fork.

## Who Is This For?

<table>
  <tr>
    <td>🧑‍💻 <b>Builders</b></td>
    <td>You want to build <i>your own</i> AI assistant without inheriting a massive codebase. Fork LightClaw, add what you need, nothing more.</td>
  </tr>
  <tr>
    <td>🎓 <b>Learners</b></td>
    <td>You want to understand how AI agents work — memory, RAG, LLM routing — in code you can read in 30 minutes.</td>
  </tr>
  <tr>
    <td>⚡ <b>Minimalists</b></td>
    <td>You need a personal AI bot on a $5/month VPS. No Docker. No build steps. Just <code>./lightclaw run</code>.</td>
  </tr>
  <tr>
    <td>🔬 <b>Tinkerers</b></td>
    <td>You want to experiment with different LLM providers, memory strategies, or prompt engineering without fighting a framework.</td>
  </tr>
</table>

## The Core Idea

> **OpenClaw is the Industrial Complex. LightClaw is the Precision Workbench.**
>
> You don't need an entire industrial complex to build a custom tool. You need a workbench with the right instruments. LightClaw gives you exactly that — a clean, readable, forkable foundation that does one thing well: **connect you to an AI through Telegram, with infinite memory.**
>
> Add Discord support? Drop in a file. Need tool calling? Add a function. Want vector search with FAISS? Swap out 20 lines in `memory.py`. The codebase is small enough that *you own it completely*.

## Features

🧠 **Infinite Memory** — Every conversation is persisted in SQLite with TF-IDF vector embeddings. The bot recalls relevant context from days, weeks, or months ago via semantic search (RAG).

🔌 **6 LLM Providers** — OpenAI (ChatGPT), xAI (Grok), Anthropic (Claude), Google (Gemini), DeepSeek, Z-AI (GLM). Switch providers by changing one line in `.env`.

📱 **Telegram Native** — Polling-based bot with "Thinking… 💭" placeholders, HTML-formatted responses, typing indicators, and rich commands.

🎭 **Customizable Personality** — Edit `.lightclaw/workspace/SOUL.md`, `IDENTITY.md`, and `USER.md` to shape your bot's character, identity, and personal context.

🧩 **Skill System (ClawHub + Local)** — Install skills from `clawhub.ai`, activate them per chat with `/skills`, and create your own custom skills locally.

🤖 **Local Agent Delegation** — Delegate large build tasks to installed local coding agents (`codex`, `claude`, `opencode`) with `/agent`, while LightClaw reports workspace change summaries back in Telegram.

🛠️ **Workspace File Operations + Diff Summaries** — Large code is written directly to `.lightclaw/workspace` (not dumped in chat). LightClaw applies create/edit operations, then returns concise operation + diff line summaries.

🧱 **Truncation Recovery for Large Files** — If an LLM response is cut mid-file, LightClaw attempts continuation/repair passes (including HTML completion) before finalizing the saved file.

🎙️ **Voice Messages** — Automatic voice transcription via Groq Whisper (optional). Send a voice note and the bot transcribes + responds.

📸 **Photo & Document Support** — Send images and files — the bot acknowledges them and processes captions through the agent loop.

🧹 **Smart Context Management** — Auto-summarization when conversations grow too long, plus emergency context window compression with retry on overflow.

📦 **Small Core** — `main.py` + `memory.py` + `providers.py` + `config.py` + `lightclaw` CLI. No hidden complexity. No abstractions for the sake of abstractions.

🚀 **Instant Startup** — No compilation, no Docker, no build pipeline. `./lightclaw run` and you're running.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      main.py                                      │
│                                                                  │
│  ┌─────────────────────────────────────────┐                     │
│  │ markdown_to_telegram_html()             │  MD → HTML converter│
│  │ load_personality()                      │  .lightclaw/workspace/*.md │
│  │ build_system_prompt()                   │  Dynamic prompts    │
│  │ transcribe_voice()                      │  Groq Whisper       │
│  └─────────────────────────────────────────┘                     │
│                                                                  │
│  LightClawBot                                                    │
│  ├── handle_message()    ← text messages                         │
│  ├── handle_voice()      ← voice transcription                   │
│  ├── handle_photo()      ← image handling                        │
│  ├── handle_document()   ← file handling                         │
│  ├── _process_user_message()                                     │
│  │     │                                                         │
│  │     ├─ 1. Send "Thinking… 💭"  placeholder                   │
│  │     ├─ 2. Recall memories      ◄── memory.py                  │
│  │     ├─ 3. Build prompt              SQLite + TF-IDF RAG      │
│  │     ├─ 4. Call LLM + retry     ◄── providers.py               │
│  │     ├─ 5. Edit placeholder          6 providers unified       │
│  │     └─ 6. Summarize if needed                                 │
│  │                                                               │
│  └── cmd_start/help/clear/wipe_memory/memory/recall/skills/agent/show │
│                                                                  │
│  config.py ◄── .env file                                          │
└──────────────────────────────────────────────────────────────────┘
```

## Quick Start

### ⚡ One-Command Setup (Recommended)

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git && cd lightclaw && bash setup.sh
```

The interactive setup wizard will walk you through:
1. Choosing your AI provider (OpenAI, xAI, Claude, Gemini, DeepSeek, Z-AI)
2. Entering your API key
3. Creating a Telegram bot via @BotFather (step-by-step guide)
4. Optional voice transcription setup
5. Auto-start your bot 🚀

### 🔧 Manual Setup

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git
cd lightclaw
pip install -r requirements.txt
```

**2. Onboard (recommended)**

```bash
./lightclaw onboard
```

This creates:
- `.env` (if missing)
- `.lightclaw/workspace/` (runtime personality files)
- `.lightclaw/lightclaw.db` (runtime DB path)

Then edit `.env` with your API key and Telegram bot token:

```env
# Choose your provider: openai | xai | claude | gemini | deepseek | zai
LLM_PROVIDER=openai
LLM_MODEL=latest
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=

# Get a token from @BotFather on Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...

# Optional: restrict to your user ID (get it from @userinfobot)
TELEGRAM_ALLOWED_USERS=123456789

# Optional tuning for large code/file generation
MAX_OUTPUT_TOKENS=12000
LOCAL_AGENT_TIMEOUT_SEC=1800

# Skills (default registry)
SKILLS_HUB_BASE_URL=https://clawhub.ai
SKILLS_STATE_PATH=.lightclaw/skills_state.json
```

**3. Customize (Optional)**

Edit the personality files in `.lightclaw/workspace/`:

```
.lightclaw/workspace/
├── IDENTITY.md   # Bot's name, purpose, philosophy
├── SOUL.md       # Personality traits and values
└── USER.md       # Your preferences and personal context
```

**4. Run**

```bash
./lightclaw run
```

That's it. Open Telegram, find your bot, say hello. 🦞

> Development mode still works with `python main.py` (it now defaults to `.lightclaw/workspace`).

## CLI Commands

```bash
lightclaw onboard   # initialize .env + .lightclaw/workspace in current directory
lightclaw onboard --reset-env  # reset existing .env from latest template
lightclaw onboard --configure  # guided provider/model/key setup on current .env
lightclaw run       # run using the current directory as runtime home
lightclaw run --provider deepseek --model deepseek-chat  # one-run provider/model override
lightclaw chat      # local terminal chat mode (same memory/workspace/provider stack)
```

`lightclaw chat` supports the same core slash commands as Telegram (`/help`, `/skills`, `/agent`, `/show`, etc.).

If `lightclaw` is not on your `PATH`, run `./lightclaw onboard` and `./lightclaw run`.

## Supported Providers

| Provider | SDK Used | Set in `.env` | Model Examples |
|----------|----------|---------------|----------------|
| **OpenAI** | `openai` | `LLM_PROVIDER=openai` | `gpt-5.2`, `gpt-5.2-mini` |
| **xAI** | `openai` (base_url override) | `LLM_PROVIDER=xai` | `grok-4-latest`, `grok-4-fast-non-reasoning` |
| **Claude** | `anthropic` | `LLM_PROVIDER=claude` | `claude-opus-4-5`, `claude-sonnet-4-5` |
| **Gemini** | `google-generativeai` | `LLM_PROVIDER=gemini` | `gemini-3-flash-preview`, `gemini-2.5-flash` |
| **DeepSeek** | `openai` (base_url override) | `LLM_PROVIDER=deepseek` | `deepseek-chat`, `deepseek-reasoner` |
| **Z-AI** | `openai` (base_url override) | `LLM_PROVIDER=zai` | `glm-5`, `glm-4.7` |

> **Pro tip:** If `LLM_MODEL` is empty, `latest`, `auto`, or `default`, LightClaw picks the latest per-provider default automatically.

Quick provider sanity test:

```bash
python scripts/provider_smoke_test.py
```

It sends a tiny prompt to each provider with a configured API key and reports `OK`/`FAIL`/`SKIP`.

## Skills (ClawHub + Local)

Install and use skills directly from Telegram:

```text
/skills search sonos
/skills add sonoscli
/skills use sonoscli
/skills off sonoscli
/skills create my_custom_skill "My private workflow"
/skills show sonoscli
```

Runtime skill paths:
- Hub skills: `.lightclaw/workspace/skills/hub/<slug>/SKILL.md`
- Local skills: `.lightclaw/workspace/skills/local/<name>/SKILL.md`

Active skills are persisted per chat in `.lightclaw/skills_state.json`.

## Local Agent Delegation

Use local coding agents for bigger project work while keeping LightClaw as the single Telegram interface:

```text
/agent
/agent use codex
/agent codex Build a complete SaaS landing page with pricing + FAQ
/agent run Build a full React dashboard in this workspace
/agent run claude Add auth + routing to the current project
/agent off
```

Supported local agents (auto-detected from `PATH`): `codex`, `claude`, `opencode`.
You should authenticate these CLIs once on the host machine before using delegation mode.

How it behaves:
- `use` enables per-chat delegation mode (normal text messages are routed to that local agent).
- `run` executes one explicit delegated task.
- After each run, LightClaw reports a compact workspace delta (created/updated/deleted files).

## Run-Time Provider Selection

When you run `lightclaw run` in a terminal, LightClaw can prompt you to choose provider + model from configured keys in `.env` (numbered choices only).

For explicit non-interactive startup, use:

```bash
lightclaw run --provider deepseek --model deepseek-chat
```

If you want a custom model ID outside the preset menu, set `LLM_MODEL` directly in `.env`.

## Workspace Code Generation & Editing

LightClaw's file pipeline is optimized for coding-heavy chats:

- Saves generated artifacts to `.lightclaw/workspace` by default.
- Accepts full-file fenced blocks, filename fences, and explicit edit hunks.
- Applies edits with exact `SEARCH/REPLACE` semantics.
- Retries failed edit hunks with current file context.
- Avoids dumping large code blocks in Telegram responses.

Supported block styles include:

````text
```html:landing/index.html
<full file content>
```
````

````text
```edit:landing/index.html
<<<<<<< SEARCH
old text
=======
new text
>>>>>>> REPLACE
```
````

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show available commands |
| `/memory` | Show memory statistics (total interactions, sessions, vocabulary) |
| `/recall <query>` | Search past conversations by semantic similarity |
| `/skills` | List/search/install/activate/create/remove skills |
| `/agent` | Delegate tasks to local coding agents (`codex`, `claude`, `opencode`) |
| `/clear` | Reset conversation history for the current chat |
| `/wipe_memory` | Wipe all saved memory (requires explicit confirmation) |
| `/wipe` | Alias of `/wipe_memory` |
| `/show` | Show current model, provider, uptime, memory stats, voice status |

## How Infinite Memory Works

Unlike traditional chatbots that forget after a session ends, LightClaw stores **every interaction** in a local SQLite database with vector embeddings:

```
User says: "I love Italian food, especially pasta"
                    │
                    ▼
          ┌─────────────────┐
          │  1. Tokenize     │  "love", "italian", "food", "pasta"
          │  2. TF-IDF Vec   │  [0.0, 0.3, 0.5, 0.7, ...]
          │  3. Store in DB   │  SQLite: content + embedding blob
          └─────────────────┘

... 3 weeks later ...

User says: "What food do I like?"
                    │
                    ▼
          ┌─────────────────┐
          │  1. Embed query  │  [0.0, 0.2, 0.6, 0.0, ...]
          │  2. Cosine sim.  │  Compare with all stored vectors
          │  3. Top-K recall │  "I love Italian food" → 0.82 sim
          │  4. Inject prompt│  System prompt gets memory context
          └─────────────────┘
                    │
                    ▼
          LLM responds: "You mentioned you love Italian food,
                         especially pasta! 🍝"
```

## Smart Context Management

LightClaw automatically manages conversation length so you never hit context window limits:

1. **Auto-summarization** — When history exceeds 20 messages or 75% of the context window, the LLM summarizes older messages while keeping the last 4 for continuity.
2. **Emergency compression** — If the LLM returns a context-too-long error, LightClaw drops the oldest 50% of messages and retries automatically.
3. **Token estimation** — Uses a 2.5 chars/token heuristic to predict when to summarize before hitting limits.
4. **Large output handling** — Uses `MAX_OUTPUT_TOKENS` and file-save pipelines to keep long code generations reliable.

## Project Structure

```
lightclaw/
├── lightclaw         # CLI entrypoint: onboard + run
├── setup.sh          # One-command interactive setup wizard
├── main.py           # Telegram bot + agent loop + HTML converter
├── skills.py         # Skills manager (ClawHub + local + per-chat activation)
├── memory.py         # SQLite infinite memory + RAG
├── providers.py      # Unified LLM client for 6 providers
├── config.py         # .env configuration
├── scripts/
│   └── provider_smoke_test.py  # Quick API smoke test for all providers
├── templates/
│   └── personality/  # Onboarding templates (IDENTITY.md, SOUL.md, USER.md)
├── .lightclaw/       # Runtime data (created by `lightclaw onboard`)
│   ├── workspace/    # Active personality files + generated artifacts
│   │   └── skills/   # Installed hub skills + local custom skills
│   ├── lightclaw.db  # Runtime memory database
│   └── skills_state.json # Per-chat active skills state
├── requirements.txt  # 6 dependencies
├── .env.example      # Configuration template
├── LICENSE           # MIT
└── .gitignore
```

That's the entire project. No `src/`. No `pkg/`. No `internal/`.

## Fork & Build Your Own

LightClaw is designed to be forked. Here are some ideas:

| What You Want | What to Change |
|---------------|----------------|
| Add Discord support | Add a Discord handler in `main.py` (~50 lines) |
| Better embeddings | Swap TF-IDF in `memory.py` for `sentence-transformers` or OpenAI embeddings |
| Tool calling | Add tool definitions to `providers.py` and a tool executor in `main.py` |
| Web search | Add a search function and inject results into the prompt |
| Multi-user personas | Extend `.lightclaw/workspace/` with per-user personality files |
| Webhook mode | Replace polling with `python-telegram-bot`'s webhook handler |
| Vision support | Send photos to GPT-5.2 or GPT-4.1 vision APIs in `handle_photo()` |

The point is: **you shouldn't need permission from a framework to add a feature**. The code is small enough to understand in an afternoon and modify with confidence.

## OpenClaw Family

| Project | Language | Purpose | Complexity |
|---------|----------|---------|------------|
| **[OpenClaw](https://github.com/openclaw/openclaw)** | TypeScript | Full-featured AI agent platform | ████████░░ |
| **[LightClaw](https://github.com/OthmaneBlial/lightclaw)** | Python | Minimal forkable agent core (6 LLMs) | ██░░░░░░░░ |

> **LightClaw** is where you start. **OpenClaw** is where you scale.

## Requirements

- Python 3.10+
- A Telegram bot token ([get one from @BotFather](https://t.me/BotFather))
- An API key from any supported LLM provider
- (Optional) Groq API key for voice transcription

## License

MIT — do whatever you want with it.

---

<div align="center">
  <p><b>🦞 LightClaw — Because the best framework is no framework.</b></p>
  <p><i>Built with ❤️ by <a href="https://github.com/OthmaneBlial">Othmane BLIAL</a></i></p>
</div>
