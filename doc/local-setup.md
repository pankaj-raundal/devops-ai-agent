# Local Setup & Running Guide

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.10+ | `sudo apt install python3` |
| pip | latest | `python3 -m ensurepip --upgrade` |
| Git | any | `sudo apt install git` |
| Azure CLI | latest | [Install guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux) |
| Azure DevOps extension | latest | `az extension add --name azure-devops` |
| GitHub CLI | latest | `sudo apt install gh` |

**Optional (for CLI-based plan generation):**

| Tool | Purpose | Install |
|------|---------|---------|
| Claude Code CLI | AI plan generation with file reading | `npm install -g @anthropic-ai/claude-code && claude login` |
| Codex CLI | AI plan generation with file reading | `npm install -g @openai/codex` |

---

## Step 1: Clone & Install

```bash
git clone <repo-url> devops-ai-agent
cd devops-ai-agent

# Create virtual environment
python3 -m venv .venv

# Activate (choose your shell):
source .venv/bin/activate          # bash/zsh
source .venv/bin/activate.fish     # fish

# Install in dev mode with all extras
pip install -e ".[dev,webhook]"
```

> **Important:** Always run commands from the `devops-ai-agent` directory, NOT from your target Drupal project directory.

## Step 2: Authenticate

```bash
# Azure DevOps (required — for story access)
az login
az devops configure --defaults organization=https://dev.azure.com/YOUR-ORG project="YOUR-PROJECT"

# GitHub CLI (required for copilot provider)
gh auth login
# Verify: gh auth token should return a token
```

## Step 3: Configure

```bash
# Copy example config
cp config/config.local.example.yaml config/config.local.yaml
```

Edit `config/config.local.yaml` with your values:

```yaml
project:
  name: "my-project"
  workspace_dir: "/path/to/your/drupal/project"
  module_path: "web/modules/contrib/your_module"
  base_branch: "master"

azure_devops:
  organization: "https://dev.azure.com/your-org"
  project: "Your Project"
  team: "Your Team"
  assigned_to: "Your Name"

ai_agent:
  provider: "copilot"          # copilot (free) | anthropic | openai
  model: "gpt-4o"
  approval_mode: "plan-review" # plan-review (safe) | auto (direct execution)
```

**Environment variables** (only if needed — see `.env` file):

```bash
# Only uncomment what you need in .env:
# ANTHROPIC_API_KEY=sk-ant-...    # If using provider: "anthropic"
# OPENAI_API_KEY=sk-...           # If using provider: "openai" or Codex CLI
# AZURE_DEVOPS_PAT=...            # Only if az CLI auth is not available
```

## Step 4: Verify Setup

```bash
# Activate venv (bash/zsh — for Fish use: source .venv/bin/activate.fish)
source .venv/bin/activate

# Check CLI is available
dai --help

# Test Azure DevOps connectivity
dai fetch
```

---

## Running the Application

### CLI Mode

```bash
# Fetch latest assigned story
dai fetch

# Run full pipeline on a single story
dai run

# Run on a specific story by ID
dai run -s 12345

# Run on all matching stories (queue mode)
dai run-all

# Run pipeline from a Zendesk ticket
dai from-zendesk 67890

# Just implement (reads .current-story.md)
dai implement

# Just run tests + AI review
dai review

# Poll and auto-run when new stories appear
dai watch
```

### Dashboard Mode

```bash
# Start the web dashboard (default: http://localhost:8090)
dai dashboard

# Custom port
dai dashboard --port 9000
```

The dashboard provides:
- Real-time pipeline progress via SSE
- Story queue panel with per-story tracking
- Plan approval modal (review AI changes before applying)
- One-click Fetch / Run / Run All controls

### Webhook Mode

```bash
# Start webhook server (receives push events from Azure DevOps / Zendesk)
dai webhook --port 8089
```

---

## Approval Modes

| Mode | Behavior |
|------|----------|
| `plan-review` (default) | AI generates a plan → you review per-file → approved changes are applied |
| `auto` | AI executes directly (CLI tools write files, API generates code) |

In `plan-review` mode, the system picks the best available tool:

1. **Claude Code CLI** (if installed) — reads files from disk, returns complete updated files
2. **Codex CLI** (if installed) — same, reads files and returns full content
3. **API fallback** — no file access, returns only new code to add; pipeline appends to existing files

---

## Running Tests

```bash
source .venv/bin/activate  # or activate.fish for Fish shell

# Run all tests
pytest tests/ -v

# Lint
ruff check src/
```

---

## Project Structure Quick Reference

```
config/
  config.yaml              # Base config (defaults)
  config.local.yaml        # Your local overrides (gitignored)
.env                        # Environment variables (gitignored)
src/
  cli.py                   # CLI entry point (dai command)
  pipeline.py              # Pipeline orchestrator
  agent/                   # AI analysis, planning, implementation
  integrations/            # Azure DevOps, Zendesk, Git, Webhooks
  reviewer/                # Test runner + AI code reviewer
  dashboard/               # Flask web UI
templates/
  implement.md             # System prompt for AI implementation
  review.md                # System prompt for AI code review
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `dai: command not found` | Run `pip install -e ".[dev]"` in the `devops-ai-agent` directory and ensure venv is activated |
| `source .venv/bin/activate` fails in Fish | Use `source .venv/bin/activate.fish` instead |
| `does not appear to be a Python project` | You're in the wrong directory — `cd` to `devops-ai-agent`, not your target project |
| `az devops: command not found` | Run `az extension add --name azure-devops` |
| No stories returned by `dai fetch` | Check `assigned_to`, `auto_tag`, `states`, and `current_sprint_only` in config |
| `gh auth token` fails | Run `gh auth login` to authenticate |
| GitHub Models API rate limit | Wait and retry, or switch to `anthropic`/`openai` provider |
| Dashboard not loading | Ensure `flask` is installed: `pip install -e ".[webhook]"` |
