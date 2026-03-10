# DevOps AI Agent

Automated pipeline that reads assigned stories from Azure DevOps (optionally triggered by Zendesk), implements changes using AI, runs tests, performs code review, and prepares a feature branch.

## Architecture

```
┌──────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────┐     ┌────────┐
│ Zendesk  │────▶│ Azure DevOps │────▶│  AI Agent    │────▶│  Tests   │────▶│ Review │
│ (ticket) │     │ (story)      │     │ (implement)  │     │ (phpunit)│     │ (AI)   │
└──────────┘     └──────────────┘     └─────────────┘     └──────────┘     └────────┘
     webhook/         WIQL query          Claude Code /       ddev exec      API-based
     polling          + REST API          Codex / API                        diff review
```

## Quick Start

```bash
# 1. Clone and install
cd devops-ai-agent
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configure
cp config/config.local.example.yaml config/config.local.yaml
cp .env.example .env
# Edit both files with your values

# 3. Login to Azure DevOps
az login
az devops configure --defaults organization=https://dev.azure.com/YOUR_ORG project="YOUR_PROJECT"

# 4. Run
dai fetch              # Fetch latest assigned story
dai run                # Full pipeline
dai run -s 1234567     # Specific work item
dai implement          # Just implement current story
dai review             # Just run tests + review
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `dai fetch` | Fetch latest assigned story from Azure DevOps |
| `dai run` | Full pipeline: fetch → branch → implement → test → review |
| `dai run -s ID` | Run pipeline for a specific work item |
| `dai implement` | Implement the current story (reads `.current-story.md`) |
| `dai review` | Run tests and AI code review on current changes |
| `dai from-zendesk ID` | Full pipeline starting from a Zendesk ticket |
| `dai watch` | Poll for new stories and auto-run pipeline |
| `dai webhook` | Start webhook server for push-based triggers |

## Configuration

Configuration is layered:
1. `config/config.yaml` — defaults
2. `config/config.local.yaml` — your overrides (git-ignored)
3. Environment variables — secrets (see `.env.example`)

### Key config sections

```yaml
project:
  name: my-project
  workspace_dir: /path/to/workspace
  module_path: web/modules/contrib/my_module

azure_devops:
  organization: my-org
  project: My Project
  team: My Team
  assigned_to: MyName

ai_agent:
  provider: anthropic          # or openai
  model: claude-sonnet-4-20250514
  implementation_strategy: auto  # auto | cli_only | api_only
```

## Trigger Options

### 1. Manual CLI
```bash
dai run
```

### 2. Polling (watch mode)
```bash
dai watch  # Polls every 5 minutes
```

### 3. Webhook (push-based)
```bash
dai webhook --port 8080
```
Configure Azure DevOps Service Hooks → Web Hooks → `http://your-server:8080/webhooks/azure-devops`

### 4. Zendesk → DevOps → Agent
```bash
dai from-zendesk 12345
```

## Project Structure

```
devops-ai-agent/
├── config/
│   ├── config.yaml              # Default config
│   └── config.local.example.yaml
├── src/
│   ├── cli.py                   # CLI entry point
│   ├── config.py                # Config loader
│   ├── pipeline.py              # Pipeline orchestrator
│   ├── agent/
│   │   ├── context_builder.py   # Story → markdown context
│   │   └── implement.py         # AI implementation agent
│   ├── integrations/
│   │   ├── azure_devops.py      # Azure DevOps client
│   │   ├── zendesk.py           # Zendesk client
│   │   ├── git_manager.py       # Git operations
│   │   └── webhook_server.py    # Flask webhook endpoints
│   ├── reviewer/
│   │   ├── test_runner.py       # PHPUnit, PHPCS, PHPStan
│   │   └── ai_reviewer.py      # AI-powered code review
│   └── utils/
│       └── __init__.py          # Logging setup
├── templates/
│   ├── implement.md             # System prompt for implementation
│   └── review.md                # System prompt for review
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

## AI Implementation Strategy

The agent tries implementation methods in order:

1. **Claude Code CLI** — Best option. Full file-level autonomy with tool use.
2. **Codex CLI** — OpenAI's coding agent.
3. **API fallback** — Sends story context + module structure to Claude/OpenAI API, gets back an implementation plan and code.

## Requirements

- Python 3.10+
- Azure CLI (`az`) with DevOps extension
- DDEV (or Docker) for local Drupal environment
- Anthropic API key or OpenAI API key
- Optional: Claude Code CLI, Codex CLI

## License

MIT
