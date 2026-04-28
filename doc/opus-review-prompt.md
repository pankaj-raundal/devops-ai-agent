# Prompt for Claude Opus 4.7 — DevOps AI Agent Review

Copy everything below the line and paste it into Claude Code (Opus 4.7).

---

## Context

I built a Python CLI tool called `devops-ai-agent` (`dai`) that automates the developer workflow:

```
Azure DevOps story → AI analysis → git branch → AI implementation → tests → AI code review → PR
```

It's designed for Drupal module developers but supports Python/React/Java/Angular projects. I want your expert review — not a code audit, but **strategic product and architecture suggestions**.

## Current Architecture

```
src/
├── cli.py              (550 lines)  — Click CLI with 12 commands
├── config.py           (80 lines)   — YAML config loader (base + local + env vars)
├── pipeline.py         (1000 lines) — Main orchestrator: fetch → analyze → branch → implement → test → review
├── history.py          (250 lines)  — SQLite run records + token usage tracking
├── profiles.py                      — Framework profiles (Drupal, Python, React, etc.)
├── agent/
│   ├── analyzer.py     (700 lines)  — Story analysis: URL fetch → Python heuristic → Claude CLI → API
│   ├── implement.py    (1500 lines) — Implementation: Claude CLI → Codex CLI → single-shot API → multi-turn tool-use
│   ├── context_builder.py (180 lines) — Builds markdown context from ADO work items
│   └── plan.py         (400 lines)  — Parse AI plans, apply file changes with merge strategies
├── integrations/
│   ├── azure_devops.py (300 lines)  — Work items via `az` CLI (WIQL queries)
│   ├── git_manager.py  (350 lines)  — GitPython: branches, commits, PRs (GitHub/ADO/GitLab)
│   ├── zendesk.py                   — Ticket polling + auto-create ADO stories
│   └── webhook_server.py           — Flask endpoints for push events
├── reviewer/
│   ├── ai_reviewer.py  (350 lines) — Python checks → Claude CLI → API review
│   └── test_runner.py  (300 lines) — Multi-framework tests (PHP/Python/JS) with container awareness
├── dashboard/
│   └── app.py                       — Flask web UI for monitoring
└── utils/
    ├── events.py       — Pub/sub event bus for live progress
    ├── progress.py     — Rich live panel (4 FPS) showing pipeline stages
    ├── rate_limit.py   — Per-provider cooldown tracking
    ├── retry.py        — Exponential backoff + provider failover
    ├── ticket_logger.py — Per-ticket audit trail
    └── data_consent.py — User consent for data sent to AI
```

## Key Design Decisions Already Made

1. **3-layer AI strategy** (everywhere): Python heuristic (free) → Claude Code CLI (Team plan, $0) → API fallback (paid per-token)
2. **CLI-first**: Using `claude -p` subprocess for analysis/review/implementation. My Claude Team plan covers it at zero marginal cost.
3. **SPIKE story support**: Auto-fetches Confluence URLs referenced in stories, feeds content to Claude CLI, produces detailed implementation plans (posted as ADO comments) without writing code.
4. **Trust levels**: cautious (approve everything) → balanced → autonomous → full-auto (zero human gates)
5. **Fix loop**: If tests fail after implementation, feeds errors back to AI for auto-fix (up to 3 attempts).
6. **History-aware retry**: Previous run errors + user corrections stored in SQLite, included in next attempt's context.
7. **Lint baseline**: Captures pre-existing lint errors before implementation, so only NEW errors are flagged.
8. **Token budgeting**: Context capped at 6k chars (free tier) or configurable higher. Tool-use turns capped at 6.
9. **Config**: `config.yaml` (base) + `config.local.yaml` (overrides) + `.env` (secrets).

## What Works Well

- Successfully processes Azure DevOps stories end-to-end (fetch → analyze → implement → test → review → PR)
- SPIKE stories: reads Confluence docs, produces detailed implementation plans with phased rollout, posts to ADO
- Python heuristic catches obvious non-code stories (zero AI cost)
- Rate limit handling with provider cooldown + CLI fallback
- Live Rich terminal UI showing pipeline progress
- SQLite history with token usage tracking per story/stage
- Run on a real Drupal module (lionbridge_translation_provider) — works for real bugs/features

## What I'm Unsure About / Want Suggestions On

### 1. Product Direction
- Currently this is a personal productivity tool. I want to make it a product other teams can use.
- Should I keep it as a CLI tool? Add a web UI? Make it a VS Code extension? SaaS?
- How should I handle the Claude Code CLI dependency? (Requires Team/Max subscription)
- What's the right pricing/distribution model for a tool like this?

### 2. Architecture Pain Points
- `implement.py` is 1500 lines with a 4-strategy fallback chain — feels fragile
- The pipeline is one big `run()` method (~300 lines) with lots of if/else branching
- Tool-use conversation history grows O(n²) per turn — expensive
- Rate limit parsing is scattered (each provider formats differently)
- Resume detection (existing branch state) has edge cases

### 3. Missing Features I'm Considering
- **Multi-repo support**: Currently handles one project at a time
- **PR review mode**: Watch for new PRs and review them automatically
- **Learning from feedback**: When developer edits AI output, capture the delta and improve future runs
- **Parallel story processing**: Currently sequential
- **Estimation**: Use analysis data to estimate story points
- **Team dashboard**: Show all team members' pipeline runs
- **Plugin system**: Let users add custom pre/post hooks per stage
- **GitHub/GitLab issue support**: Currently ADO-only for story tracking

### 4. AI Strategy Questions
- Should I move from Claude CLI subprocess to the Anthropic API with tool-use for everything?
- Is the 3-layer strategy (Python → CLI → API) worth the complexity?
- How should I handle context windows for large codebases (>100 files)?
- Should I add RAG (embed codebase → vector search → relevant files) instead of keyword matching?

### 5. Testing & Quality
- Currently 113 unit tests but no integration tests
- No end-to-end tests (would need mock ADO + real git repo)
- No performance benchmarks (how many stories/hour, token costs per story)

## Stats from Real Usage
- Target workspace: Drupal module with ~50 PHP files
- Average story analysis: 3-5 seconds (Python heuristic) or 30-120s (Claude CLI)
- Average implementation: 2-10 minutes depending on complexity
- Token usage: ~2k-5k for analysis, ~5k-20k for implementation, ~2k-5k for review
- Fix loop: Usually 0-1 attempts needed; max 3

## What I Want from You

1. **Product strategy**: What would make this a viable product? What's the moat?
2. **Architecture improvements**: What would you refactor first? What patterns would you introduce?
3. **Feature prioritization**: Of the features I'm considering, which 3 would have the most impact?
4. **AI strategy**: Should I change my approach to AI integration? What's the optimal architecture?
5. **Scaling concerns**: What will break first as this goes from 1 user to 100?
6. **Competitive landscape**: What similar tools exist and how is this different?
7. **Anything else**: What am I not thinking about that I should be?

Be specific and opinionated. I don't want hedging — tell me what you'd actually do if this were your project.
