---
applyTo: "**"
---

# DevOps AI Agent - Development Context

## Project Overview
- **Name:** `devops-ai-agent`
- **Purpose:** Automated pipeline that reads assigned stories from Azure DevOps (optionally triggered by Zendesk), implements changes using AI, runs tests, performs code review, and prepares a feature branch.
- **Language:** Python 3.10+
- **Package Manager:** pip (pyproject.toml)
- **CLI Entry Point:** `dai` (Click-based, defined in `src/cli.py`)
- **License:** MIT

## Architecture

```
Zendesk (ticket) → Azure DevOps (story) → AI Agent (implement) → Tests → AI Review
```

### Module Structure
```
src/
├── cli.py                   # Click CLI — dai fetch/run/implement/review/watch/webhook
├── config.py                # YAML config loader with env var overrides
├── pipeline.py              # End-to-end orchestrator (fetch → branch → implement → test → review)
├── agent/
│   ├── context_builder.py   # WorkItem → markdown context for AI
│   └── implement.py         # AI implementation (Claude Code CLI → Codex CLI → API fallback)
├── integrations/
│   ├── azure_devops.py      # AzureDevOpsClient — WIQL queries, work item CRUD via `az` CLI
│   ├── zendesk.py           # ZendeskClient — ticket polling + REST API via httpx
│   ├── git_manager.py       # GitManager — branch creation, commit, diff via GitPython
│   └── webhook_server.py    # Flask webhook endpoints for Azure DevOps + Zendesk push events
├── reviewer/
│   ├── test_runner.py       # Runs phpunit, phpcs, phpstan, drush cr via subprocess
│   └── ai_reviewer.py       # Sends git diff to Claude/OpenAI for automated code review
└── utils/
    └── __init__.py          # Logging setup (console + file handlers)
```

## Configuration
- **Default config:** `config/config.yaml`
- **Local overrides:** `config/config.local.yaml` (git-ignored)
- **Secrets via env vars:** `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN` (for copilot provider), `AZURE_DEVOPS_PAT`, `ZENDESK_API_TOKEN`, `WEBHOOK_SECRET`, `SLACK_WEBHOOK_URL`
- Config loading: `src/config.py` — `load_config()` merges base → local → env vars

## Key Dependencies
- `click` — CLI framework
- `rich` — Terminal formatting (tables, colors)
- `httpx` — Async HTTP client (Zendesk API)
- `anthropic` / `openai` — AI API clients
- `gitpython` — Git operations
- `pyyaml` — Config parsing
- `flask` — Webhook server (optional)
- `watchdog` — File system watching (optional)

## CLI Commands
| Command | Description |
|---------|-------------|
| `dai fetch` | Fetch latest assigned story from Azure DevOps |
| `dai run [-s ID] [--skip-tests]` | Full pipeline: fetch → branch → implement → test → review |
| `dai implement` | Implement current story (reads `.current-story.md`) |
| `dai review` | Run tests + AI code review on current changes |
| `dai from-zendesk TICKET_ID` | Full pipeline starting from Zendesk ticket |
| `dai watch` | Poll for new stories and auto-run pipeline |
| `dai webhook [--port PORT]` | Start Flask webhook server |

## Azure DevOps Integration
- Uses `az` CLI (not REST API directly) for WIQL queries and work item operations
- Org: configured via `azure_devops.organization` in config
- WIQL queries filter by: assigned_to (CONTAINS), current sprint (`@CurrentIteration`), auto tag, state
- Comments fetched via `az devops invoke --area wit --resource updates`

## AI Implementation Strategy (in order)
1. **Claude Code CLI** (`claude --print`) — best option, full file-level autonomy
2. **Codex CLI** (`codex --approval-mode auto`) — OpenAI alternative
3. **API fallback** — sends story context + module structure to Claude/OpenAI/GitHub Copilot API
   - Providers: `anthropic` (Anthropic SDK), `openai` (OpenAI SDK), `copilot` (GitHub Models API — OpenAI-compatible, uses `GITHUB_TOKEN`)

## Development Workflow
1. Activate venv: `source .venv/bin/activate`
2. Install in dev mode: `pip install -e ".[dev]"`
3. Run tests: `pytest tests/ -v`
4. Lint: `ruff check src/`
5. Run CLI: `dai --help`

## Coding Standards
- Follow PEP 8 / Python best practices
- Use type hints (Python 3.10+ syntax: `X | None`, `list[str]`)
- Use `from __future__ import annotations` for forward references
- Use `dataclass` for data containers
- Use `logging` module (logger per module: `logging.getLogger("devops_ai_agent.xxx")`)
- Use `subprocess.run()` with `capture_output=True, text=True` for CLI commands
- Keep functions focused — single responsibility
- No hardcoded secrets — always use config/env vars

## Testing
- Framework: pytest
- Test files: `tests/test_*.py`
- Run: `pytest tests/ -v`
- Config in `pyproject.toml` under `[tool.pytest.ini_options]`

## Target Use Case
This tool is designed for Drupal module developers using DDEV who work with Azure DevOps for story tracking. It automates the tedious cycle of: read story → create branch → implement → test → review.
