# Implementation Summary — Phases 1-5

> **Date:** April 1, 2026
> **Total test count:** 95 passing
> **All changes:** Engine-first roadmap addressing 10 critical review findings

---

## What Was Built

### Phase 1: Self-Correcting Pipeline
**Problem:** Pipeline failed at first test error and stopped. Human had to diagnose, fix, and re-run.

**Solution:**
- **Iterative fix loop** — when tests fail, auto-fix lint errors first (phpcbf, ruff --fix, eslint --fix), then feed remaining errors back to AI for up to 3 fix attempts before giving up
- **Trust levels** — 4 modes (`cautious` → `full-auto`) that progressively remove manual gates (consent, plan approval, push confirmation) to enable unattended batch processing
- **Consent persistence** — remembers AI provider approval in `.dai/consent.json` so users aren't prompted every run
- **Story quality scoring** — scores stories 1-10 before spending AI tokens; low-quality stories get coaching feedback posted back to Azure DevOps instead of wasted implementation attempts

**Files changed:** `pipeline.py`, `test_runner.py`, `implement.py`, `ai_reviewer.py`, `data_consent.py`, `config.yaml`

---

### Phase 2: Context-Aware Implementation
**Problem:** API-mode AI had no filesystem access — it guessed file contents, producing code that landed in wrong places or destroyed existing code.

**Solution:**
- **Two-pass context injection** — Pass 1: AI selects which files are relevant. Pass 2: pipeline reads those files from disk (with path traversal protection, 30K char budget) and includes actual content in the prompt
- **Smart merge** — PHP files: inserts new code before the class closing brace. Python: inserts before `if __name__` guard. Falls back to append for other languages
- **Auto-replace for small files** — files under 500 lines where AI returns what looks like a complete file (detected via language-specific heuristics) automatically upgrade from "append" to "replace" strategy

**Files changed:** `implement.py`, `plan.py`

---

### Phase 3: Multi-Framework & Multi-Platform
**Problem:** Only Drupal/PHP worked. Test runners were hardcoded to phpunit/phpcs. PRs only worked with GitHub.

**Solution:**
- **14 test runner handlers** — `pytest`, `ruff`, `mypy`, `jest`, `eslint`, `tsc`, `ng_test`, `ng_lint`, `mvn_test`, `checkstyle`, `spotbugs`, `dotnet_test`, `dotnet_format`, `dotnet_build`
- **Profile-driven extensions** — lintable file extensions pulled from framework profiles (`.py`, `.ts`, `.java`, `.cs`, etc.) instead of hardcoded `.php`
- **Container-aware commands** — `_needs_container()` checks env type; DDEV/Lando/Docker Compose prepend `<container> exec`, native runs directly
- **Multi-platform PRs** — detects GitHub/Azure DevOps/GitLab from `git remote get-url origin`, routes to `gh pr create`, `az repos pr create`, or `glab mr create`

**Files changed:** `test_runner.py`, `git_manager.py`

---

### Phase 4: Batch Automation & Learning
**Problem:** Run history was a 10-entry JSON file. No way to run multiple stories overnight. AI repeated the same mistakes on retry.

**Solution:**
- **SQLite run history** (`src/history.py`) — replaces JSON with queryable database. Stores trust level, provider, fix attempts, errors. Unlimited records, indexed by work item
- **History-aware retry** — `build_history_context()` injects past errors AND user rejection feedback into AI context so it doesn't repeat mistakes
- **Rejection feedback storage** — `feedback` table captures file-level user corrections (diffs + comments) linked to specific runs
- **Overnight batch mode** — `dai run-all --trust full-auto` overrides trust level, prints `generate_batch_summary()` report at end
- **CI/CD templates** — ready-to-copy GitHub Actions and Azure Pipelines YAML for nightly batch runs
- **`dai history` command** — CLI table view of run history, filterable by story ID

**Files changed:** `history.py` (new), `pipeline.py`, `cli.py`, `templates/cicd/github-actions.yml`, `templates/cicd/azure-pipelines.yml`

---

### Phase 5: Onboarding & Setup
**Problem:** New users had to manually create YAML config, discover 15+ required fields, install CLIs, and authenticate services — all with no guidance until runtime failure.

**Solution:**
- **`dai init`** — interactive setup wizard that auto-detects framework (composer.json→Drupal, pyproject.toml→Python, package.json→React/Angular, pom.xml→Java, .csproj→.NET), environment type (.ddev, .lando.yml, docker-compose.yml), and git base branch. Prompts for remaining fields and generates `config.local.yaml`
- **`dai doctor`** — 10-point health check: Python ≥3.10, git installed, workspace exists and is git repo, module path exists, base branch exists, Azure CLI + azure-devops extension, ADO org/project configured, AI provider API key set, container tool in PATH, GitHub CLI auth. Each failure includes a specific fix action

**Files changed:** `setup.py` (new), `cli.py`

---

## Why Phase 6 Is Deferred

Phase 6 covers **Dashboard improvements**: read-only diff view in plan modal, editable plan content via CodeMirror, and per-run state isolation for concurrent pipeline runs.

### The Gate

Phase 6 is blocked by a validation gate:

> **Phase 1-4 must demonstrate that the engine can process 5+ stories overnight with passing tests.**

This gate exists because:

1. **The dashboard is a viewer, not the engine.** Making the viewer prettier while the engine can't reliably produce working code is wasted effort. The critical review specifically called this out: *"the dashboard is a monitor for a factory that doesn't reliably produce output."*

2. **The strategic positioning is "coding pipeline, not coding assistant."** The value proposition is overnight batch processing — a developer queues 15 stories before leaving and finds 10 done by morning. That value comes entirely from the engine (Phases 1-5), not from CodeMirror integration in a browser tab.

3. **Phase 6 items are polish, not capability.** Diff views, editable plans, and concurrent run isolation improve the experience for developers who are actively watching. But the target persona — the team lead who runs `dai run-all --trust full-auto` from a CI pipeline — doesn't use the dashboard at all.

### What Unlocks Phase 6

Run `dai run-all --trust full-auto` against a real project with 5+ stories in the sprint backlog. If 3+ produce branches with passing tests, the engine works and dashboard investment is justified. Until then, the engine needs tuning, not a better UI.

### Phase 6 Items (When Ready)

| # | Item | Effort |
|---|------|--------|
| 20 | Read-only diff view in plan modal | ~80 lines |
| 21 | Editable plan content (CodeMirror) | ~150 lines |
| 22 | Per-run state isolation (concurrent runs) | ~60 lines |

---

## CLI Commands Summary

| Command | Description | Phase |
|---------|-------------|-------|
| `dai init` | Interactive setup wizard | 5 |
| `dai doctor` | Environment health check | 5 |
| `dai fetch` | Fetch latest assigned story | — |
| `dai run [-s ID]` | Full pipeline for one story | — |
| `dai run-all [--trust full-auto]` | Batch pipeline for all stories | 4 |
| `dai implement` | Implement current story | — |
| `dai review` | Run tests + AI review | — |
| `dai history [-s ID] [-n LIMIT]` | View run history | 4 |
| `dai from-zendesk TICKET_ID` | Pipeline from Zendesk ticket | — |
| `dai watch` | Poll for new stories | — |
| `dai webhook` | Start webhook server | — |
| `dai dashboard` | Start web dashboard | — |

---

## Test Coverage

| Phase | New Tests | Cumulative |
|-------|-----------|------------|
| 1 | 0 (existing 8) | 8 |
| 2 | 18 | 26 |
| 3 | 28 | 54 |
| 4 | 12 | 66 |
| 5 | 29 | 95 |

---

## Getting Started — New User Guide

This section walks you through setting up and using `dai` from scratch.

### Prerequisites

| Requirement | Why |
|-------------|-----|
| **Python 3.10+** | Runtime for the CLI |
| **Git** | Branch creation, commits, diffs |
| **Azure DevOps account** | Story tracking (you need a PAT with work item read/write) |
| **Azure CLI** (`az`) + `azure-devops` extension | Fetching and updating stories |
| **An AI provider API key** | At least one of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GITHUB_TOKEN` |
| **GitHub CLI** (`gh`) *(optional)* | Auto-create Pull Requests on GitHub repos |

### Step 1: Install

```bash
# Clone the repo
git clone <repo-url> devops-ai-agent
cd devops-ai-agent

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode
pip install -e ".[dev]"

# Verify
dai --help
```

### Step 2: Run the Setup Wizard

```bash
dai init
```

The wizard auto-detects your project and walks you through the configuration:

1. **Workspace directory** — path to your local project (e.g., `/home/you/projects/my-drupal-site`)
2. **Framework** — auto-detected from project files (`composer.json` → Drupal, `pyproject.toml` → Python, `package.json` → React/Angular, `pom.xml` → Java, `*.csproj` → .NET)
3. **Module/source path** — the specific package or module you're working on (relative to workspace)
4. **Azure DevOps org, project, team** — where your stories live
5. **AI provider** — `anthropic`, `openai`, or `copilot` (GitHub Models)
6. **Environment type** — auto-detected (DDEV, Lando, Docker Compose, or native)

This creates `config/config.local.yaml` with your settings.

### Step 3: Set Environment Variables

Export the secrets that the wizard can't store in YAML:

```bash
# Pick your AI provider:
export ANTHROPIC_API_KEY="sk-ant-..."       # if using Anthropic
export OPENAI_API_KEY="sk-..."              # if using OpenAI
export GITHUB_TOKEN="ghp_..."              # if using GitHub Copilot (GitHub Models API)

# Azure DevOps access:
export AZURE_DEVOPS_PAT="your-personal-access-token"
```

### Step 4: Check Your Environment

```bash
dai doctor
```

Runs 10 health checks and tells you exactly what's missing:

- Python version ≥ 3.10
- Git installed
- Workspace exists and is a git repo
- Module path exists
- Base branch exists
- Azure CLI + azure-devops extension installed
- Azure DevOps org/project configured
- AI provider API key set
- Container tool (ddev/lando/docker) in PATH
- GitHub CLI authenticated (for auto-PR)

Fix any red items before proceeding.

### Step 5: Run Your First Story

There are two ways to start:

#### Option A: Let `dai` pick the next assigned story

```bash
dai run
```

This runs the full pipeline:
1. Fetches the latest story assigned to you from Azure DevOps (with the `auto` tag)
2. Creates a feature branch (`feature/12345-story-title`)
3. Scores the story quality (rejects poorly written stories with coaching feedback)
4. Sends the story context + relevant source files to AI for implementation
5. Shows you the plan — you review and approve each file change
6. Runs tests (phpunit, pytest, jest, etc. depending on your framework)
7. If tests fail, auto-fixes lint errors first, then asks AI to fix remaining issues (up to 3 attempts)
8. Runs AI code review on the final diff
9. Commits and (optionally) pushes the branch

#### Option B: Run a specific story by ID

```bash
dai run -s 12345
```

#### Option C: Start from a Zendesk ticket

```bash
dai from-zendesk TICKET_ID
```

### Step 6: Review the Output

After the pipeline finishes:

```bash
# See what changed
git diff main..HEAD

# Check the AI review summary (printed to terminal)

# Push when satisfied
git push origin HEAD
```

### Understanding Trust Levels

Trust levels control how much manual approval is required:

| Level | Consent | Plan Review | Push Confirm | Best For |
|-------|---------|-------------|--------------|----------|
| `cautious` | ✅ | ✅ | ✅ | First-time use, learning the tool |
| `balanced` | ❌ | ✅ | ✅ | Daily interactive use |
| `autonomous` | ❌ | ❌ | ✅ | Confident in AI output, want speed |
| `full-auto` | ❌ | ❌ | ❌ | CI/CD pipelines, overnight batch runs |

Change trust level in `config/config.local.yaml`:

```yaml
ai_agent:
  trust_level: "balanced"
```

### Batch Mode — Process Multiple Stories Overnight

```bash
# Process all assigned stories with no manual gates
dai run-all --trust full-auto
```

Prints a summary report at the end showing which stories succeeded/failed.

### Useful Commands

```bash
# Fetch the next story without running the pipeline
dai fetch

# Re-implement the current story (reads .current-story.md)
dai implement

# Run tests + AI review on current changes only
dai review

# View past run history
dai history

# View history for a specific story
dai history -s 12345

# Poll for new stories and auto-run (long-running)
dai watch
```

### Typical Daily Workflow

```
Morning:
  1. dai run                     # pick up the next story
  2. Review the plan → approve   # AI shows what it will change
  3. Tests pass? → push          # or fix manually and push

Evening (optional):
  1. dai run-all --trust full-auto   # queue remaining stories
  2. Check results next morning
```
