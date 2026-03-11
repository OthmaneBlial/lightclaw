# Multi-Agent Guide

`/agent multi` is LightClaw's lightweight orchestration mode for splitting one goal into multiple worker lanes.

In one nutshell:

- You give one goal.
- LightClaw proposes a multi-worker plan.
- You confirm, edit, or cancel it.
- Workers run in parallel when safe.
- Each worker writes handoff files.
- LightClaw checks the outputs with lightweight acceptance and cross-lane audits.

This is not a heavy framework.
It stays local, file-based, and practical.

## Core Commands

Start a plan:

```text
/agent multi <goal>
```

Prefer agents:

```text
/agent multi @claude @codex <goal>
```

Force explicit lane assignment:

```text
/agent multi --agent backend=claude --agent frontend=codex --agent docs=claude <goal>
```

Add explicit DAG dependencies when you need guaranteed ordering:

```text
/agent multi --agent backend=codex --agent frontend=claude --agent integration=claude --depends-on integration=backend,frontend build the app
```

`--depends-on` also works as `--depends-on=integration=backend,frontend`.

Explicit rosters still accept dependency hints in the goal text when you do not pass `--depends-on`, for example:

```text
/agent multi --agent backend=codex --agent frontend=claude --agent integration=claude build the app, keep backend and frontend parallel, and make integration wait for backend and frontend
```

Run the proposed plan:

```text
/agent multi confirm
```

Regenerate the plan with feedback:

```text
/agent multi edit make docs a final lane and keep backend/frontend parallel
```

Cancel the plan:

```text
/agent multi cancel
```

## How It Thinks

`/agent multi` usually creates:

- coding lanes for implementation, validation, docs, or integration
- research lanes for findings and synthesis
- authoring lanes for articles, reports, and deliverables
- review lanes for critique, gap detection, and recommendations

Each lane gets:

- responsibilities
- expected inputs
- expected outputs
- owned paths when relevant
- acceptance checks
- `handoff/<lane>.md`
- `handoff/<lane>.json`

## What Makes It Strong

- true DAG scheduling: downstream lanes start as soon as their own deps are done
- machine-readable handoffs
- lightweight repair attempts
- contract-aware checks like `outputs.endpoints`, `outputs.api_calls`, `outputs.findings`, `outputs.deliverables`
- global audits for API, findings flow, and deliverables

## Best Use Cases

- full-stack app builds
- backend/frontend/docs splits
- research + review workflows
- article/report pipelines
- migration planning
- bug triage and repair
- implementation + QA
- architecture + execution

## Example Library

### Quick Start

```text
/agent multi build a tiny todo app with a FastAPI backend and a simple frontend
```

```text
/agent multi research the best local-first note taking architectures and summarize tradeoffs
```

```text
/agent multi write a product launch article for LightClaw and include a short FAQ
```

### Coding: Full Stack

```text
/agent multi build a tiny dashboard app with a FastAPI backend under backend/, a frontend under frontend/, and docs under docs/ with clear run steps
```

```text
/agent multi build a small CRM app with a Python backend, React frontend, and setup docs
```

```text
/agent multi build a URL shortener with API, web UI, and README
```

```text
/agent multi build a local file upload app with auth, admin page, and test coverage
```

```text
/agent multi build a markdown notes app with search, tagging, and documentation
```

### Coding: Explicit Roster

```text
/agent multi --agent backend=claude --agent frontend=claude --agent docs=claude build a tiny dashboard app with a FastAPI backend under backend/, a frontend under frontend/, and docs under docs/ with clear run steps
```

```text
/agent multi --agent architect=claude --agent backend=codex --agent frontend=claude --agent qa=codex build a small expense tracker
```

```text
/agent multi --agent research=claude --agent builder=codex --agent reviewer=claude build a CLI for log analysis
```

```text
/agent multi --agent backend=codex --agent integration=claude --agent docs=claude create a webhook processing service with deployment notes
```

```text
/agent multi --agent market_research=claude --agent aso_audit=claude --agent competitor_analysis=claude --agent keyword_research=claude --agent ux_review=claude --agent product_positioning=claude --agent growth_strategy=claude --agent monetization_review=claude --agent technical_audit=claude --agent acquisition_strategy=claude --agent master_strategy=claude --depends-on master_strategy=market_research,aso_audit,competitor_analysis,keyword_research,ux_review,product_positioning,growth_strategy,monetization_review,technical_audit,acquisition_strategy investigate why the Android app is getting almost no installs
```

### Coding: Bug Fixing

```text
/agent multi fix a flaky login flow in this repo, add regression coverage, and document the root cause
```

```text
/agent multi debug why the frontend shows empty data, fix the contract mismatch, and update docs
```

```text
/agent multi repair the test suite, remove broken mocks, and summarize the regressions found
```

```text
/agent multi investigate startup slowness, patch the bottleneck, and report the before/after impact
```

### Coding: Refactors

```text
/agent multi refactor this monolith into clearer modules, keep behavior the same, and add migration notes
```

```text
/agent multi reorganize the frontend into feature folders, preserve behavior, and add developer docs
```

```text
/agent multi split database access, API routes, and schemas into cleaner backend modules
```

### Coding: Quality and Review

```text
/agent multi review this repository for correctness, patch the high-severity issues, and leave docs for the rest
```

```text
/agent multi add tests around the payment flow, verify edge cases, and document remaining risks
```

```text
/agent multi harden this API for bad input, add validation coverage, and update the API reference
```

### Research and Analysis

```text
/agent multi research the best self-hosted AI agent architectures for small teams and produce a decision brief
```

```text
/agent multi analyze the open-source chatbot landscape and recommend where LightClaw should differentiate
```

```text
/agent multi compare Claude Code, Codex CLI, and OpenClaw style workflows for local coding agents
```

```text
/agent multi investigate the best memory strategies for long-running AI chats and produce findings plus recommendations
```

```text
/agent multi research local-first product ideas for developers and rank the top 10 by implementation effort and market pull
```

```text
/agent multi analyze why many multi-agent systems become too heavy and propose a lightweight design checklist
```

### Research with Explicit Roles

```text
/agent multi --agent research=claude --agent reviewer=claude research the competitive landscape for local AI coding assistants and produce a brief
```

```text
/agent multi --agent analyst=claude --agent reviewer=codex analyze developer pain points around prompt engineering and synthesize findings
```

```text
/agent multi --agent research=claude --agent author=claude --agent reviewer=codex produce a research-backed report on AI tooling for indie hackers
```

### Docs and Documentation

```text
/agent multi document this repository for new contributors with setup steps, architecture notes, and troubleshooting
```

```text
/agent multi create a complete API reference, setup guide, and project overview for this codebase
```

```text
/agent multi turn this rough codebase into something a new engineer can onboard into in 15 minutes
```

```text
/agent multi write migration docs for moving from a single-agent flow to /agent multi in LightClaw
```

### Content and Writing

```text
/agent multi write a blog post explaining how LightClaw keeps multi-agent orchestration lightweight
```

```text
/agent multi create a launch article, release notes, and FAQ for the new multi-agent feature
```

```text
/agent multi write a deep-dive article on DAG scheduling for local coding agents
```

```text
/agent multi create a practical guide for teams adopting local AI agents without adding heavy infra
```

```text
/agent multi write a report on the future of local AI workflows and a short executive summary
```

### Product and Strategy

```text
/agent multi define the roadmap for LightClaw multi-agent mode over the next 3 milestones
```

```text
/agent multi produce a product brief for making /agent multi the flagship feature for LightClaw
```

```text
/agent multi analyze pricing and packaging strategies for a hosted version of LightClaw
```

```text
/agent multi research user segments for LightClaw and write positioning recommendations
```

### Mixed Workflows

```text
/agent multi research the best UX for a dashboard app, build a first version, and document how it works
```

```text
/agent multi analyze bug reports in this repo, fix the top issues, and write a postmortem
```

```text
/agent multi research the best schema for a notes app, implement it, and generate an API reference
```

```text
/agent multi audit this codebase for architecture problems, refactor the worst area, and explain the new structure
```

```text
/agent multi investigate why onboarding is confusing, rewrite the docs, and suggest product improvements
```

### Prompting for Better Plans

If you want better plans, ask with structure:

```text
/agent multi build a tiny support ticket app, keep backend and frontend parallel, make docs the final lane, and prefer simple local storage
```

```text
/agent multi research local AI agent products, keep one lane for raw findings and one lane for critique, then produce a final brief
```

```text
/agent multi write a launch article, keep research separate from authoring, and make the reviewer focus on unsupported claims
```

### Edit Examples

After a proposed plan appears:

```text
/agent multi edit add a final docs lane
```

```text
/agent multi edit keep backend and frontend parallel, but make integration wait for both
```

```text
/agent multi edit use a research lane first, then an author lane, then a reviewer lane
```

```text
/agent multi edit reduce the worker count to 2 and keep it lightweight
```

```text
/agent multi edit give the reviewer final responsibility for caveats and recommendations
```

### Good Patterns

- Use broad natural goals first.
- Add explicit lane structure only when you know the split you want.
- Use `edit` to improve the plan instead of over-specifying the first prompt.
- For non-code work, ask for research, authoring, review, docs, or report lanes explicitly.
- For code work, ask for backend, frontend, qa, docs, or integration when the split matters.

### Lightweight Patterns

Good:

```text
/agent multi build a small webhook receiver with docs
```

```text
/agent multi research the best memory model for a local chat assistant and summarize the tradeoffs
```

Avoid starting too heavy:

```text
/agent multi create 5 workers for a tiny script
```

Better:

```text
/agent multi build a tiny script, keep the plan to 2 or 3 workers max
```

## What To Look For In Results

Good signs:

- lanes start in parallel when they should
- docs or review waits for upstream lanes
- handoff JSON is populated with real machine-readable fields
- audits pass
- the final report clearly tells you where lanes disagree

Warning signs:

- one lane invents a different API or data model
- docs contradict code
- handoff JSON is present but empty or vague
- audits fail but you ignore them

## Practical Workflow

1. Start with `/agent multi <goal>`.
2. Read the proposed lanes.
3. Use `/agent multi edit ...` if the split is wrong.
4. Confirm.
5. Read the final audits, not just the worker summaries.
6. If an audit fails, rerun with tighter wording or fix the repo and try again.

## Inspiration Prompts

```text
/agent multi build the smallest useful version of a customer portal
```

```text
/agent multi research underserved workflows for indie developers and turn the findings into product ideas
```

```text
/agent multi create a clean internal engineering guide for this repo
```

```text
/agent multi analyze where this codebase leaks complexity and propose a simplification plan
```

```text
/agent multi write a brutally practical launch article for LightClaw multi-agent mode
```

```text
/agent multi compare three possible directions for LightClaw and recommend the one with the best leverage
```

```text
/agent multi redesign this project structure, keep it lightweight, and document the new mental model
```
