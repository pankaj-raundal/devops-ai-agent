# Architectural Response to Critical Review

> **Date:** March 2026
> **Author:** Senior Architect
> **Purpose:** Actionable technical plan to address every issue raised in [critical-review.md](critical-review.md)
> **Philosophy:** Don't compete with Copilot-in-editor. Own the space Copilot can't touch: **unattended batch processing, end-to-end traceability, and DevOps lifecycle automation.**

---

## Strategic Positioning (Before We Fix Anything)

The critic compared us to "Copilot in VS Code" — an interactive, developer-in-the-loop assistant. That's the wrong comparison and we fell into the trap by trying to be both.

**We should NOT be a coding assistant. We should be a coding pipeline.**

| Copilot in VS Code | DevOps AI Agent |
|---|---|
| Developer is present, typing | Developer is asleep or in meetings |
| One file at a time | Full story → branch → implement → test → review → PR |
| No project management integration | ADO story lifecycle, Zendesk ticketing |
| No audit trail | Full traceability: who requested → what AI did → what tests passed → who approved |
| Manual branch/PR/state management | Fully automated DevOps lifecycle |

**Target persona shift:** Not "developer coding at desk" but "team lead who has 15 low-complexity stories in sprint backlog and wants 10 of them done by morning."

With this framing, here's how we address every issue:

---

## Issue #1: Setup Tax Is Way Too High

### Root Cause
No `dai init` command. No environment validation. User must manually copy YAML, fill 15+ fields, install CLIs, authenticate services — all undiscovered until runtime failure.

### Approach

**A. Interactive Setup Wizard — `dai init`**

Add a CLI command that walks through setup interactively:

```
$ dai init
Welcome to DevOps AI Agent setup!

? Project framework: [drupal/python/react/java/dotnet/angular]
? Workspace directory: /home/user/myproject (auto-detected from cwd)
? Module/source path: web/modules/contrib/my_module
? Azure DevOps org: my-org
? Azure DevOps project: my-project
? AI provider: [anthropic/openai/copilot]

Checking environment...
  ✓ Python 3.12
  ✓ Git 2.40
  ✓ Azure CLI (az) — authenticated as user@org.com
  ✓ GitHub CLI (gh) — authenticated
  ✗ DDEV — not found (tests will be skipped unless installed)
  ✓ GITHUB_TOKEN — set

Writing config/config.local.yaml...
Done! Run 'dai fetch' to verify.
```

**Implementation:**
- New `src/cli.py` command: `dai init`
- Use `click.prompt()` with choices and defaults
- Auto-detect: `cwd` for workspace, `git remote` for org, `az account show` for auth check
- Validate each dependency: `which az`, `which gh`, `which ddev`, env var checks
- Generate `config.local.yaml` from answers
- Estimated effort: ~150 lines, 1 new function

**B. `dai doctor` — Environment Health Check**

A validation command users can run anytime:

```
$ dai doctor
Config:       ✓ config.local.yaml loaded
Workspace:    ✓ /home/user/myproject (git repo, clean)
Azure DevOps: ✓ Connected (org: my-org, project: my-project)
AI Provider:  ✓ copilot (GITHUB_TOKEN valid, model: gpt-4o)
Test Runner:  ✗ DDEV not running (run 'ddev start')
PR Creation:  ✓ gh CLI authenticated
```

**Implementation:**
- New `src/cli.py` command: `dai doctor`
- Calls each integration's connectivity check
- Rich table output with ✓/✗ status
- Estimated effort: ~100 lines

---

## Issue #2: Stories Must Be "AI-Ready"

### Root Cause
Pipeline assumes stories have clear descriptions and acceptance criteria. Vague stories produce vague implementations. No quality gate at story intake.

### Approach

**A. Story Quality Score in Analysis Stage**

The AI analysis stage (`StoryAnalyzer`) already evaluates stories. Extend it to produce a **quality score**:

```json
{
  "story_quality": {
    "score": 3,
    "max": 10,
    "issues": [
      "No acceptance criteria defined",
      "Description is less than 20 words",
      "No specific files or modules mentioned"
    ],
    "suggestion": "Add acceptance criteria: 'When X happens, Y should change to Z'"
  }
}
```

**Behavior:**
- Score < 4 → Pipeline pauses, posts a comment back to ADO: *"This story needs more detail for AI implementation. Missing: acceptance criteria, expected behavior."* → Transitions story to "Needs Info" state instead of implementing
- Score 4-6 → Pipeline continues but flags low confidence in ADO comment
- Score 7+ → Full confidence, proceed

**This turns the problem into a feature:** the tool actively coaches PMs to write better stories.

**B. Story Enhancement Prompt**

When quality is low, offer an option to have the AI *expand* the story before implementing:

```
Story #1234 has low quality (3/10). Missing: acceptance criteria.
? Would you like AI to expand this story before implementing? [Y/n]
```

AI drafts better acceptance criteria based on codebase context → posts as a suggested comment to ADO → waits for PM approval before proceeding.

**Implementation:**
- Extend `AnalysisResult` dataclass with `story_quality` fields
- Add quality thresholds to config: `ai_agent.min_story_quality: 4`
- Add ADO state `state_on_needs_info: "Needs Information"` to config
- Estimated effort: ~60 lines in `analyzer.py`, ~20 lines in `pipeline.py`

---

## Issue #3: The AI Can't See the Codebase (API Fallback)

### Root Cause
`_get_module_summary()` returns only a file tree (names only). API mode prompt says "provide ONLY the new code to ADD" with `merge_strategy=append`. AI is coding blind.

### Approach

**A. Smart Context Injection — Send Relevant File Contents**

Instead of sending the entire module (token-expensive), send **only the files the AI is likely to modify**:

```python
def _get_targeted_context(self, story_context: str, module_dir: Path) -> str:
    """Use AI's analysis to identify target files, then read their contents."""
    # Step 1: Ask AI which files it needs to see (cheap, fast call)
    file_list = self._get_module_summary(module_dir)
    selection_prompt = f"""Given this story and file tree, list the 5-10 files
    you would need to read to implement this change. Return only file paths.

    Story: {story_context[:2000]}
    Files: {file_list}"""

    selected_files = self._quick_ai_call(selection_prompt)

    # Step 2: Read those files and include in implementation prompt
    context_parts = []
    total_tokens = 0
    for filepath in selected_files:
        full_path = module_dir / filepath
        if full_path.exists() and full_path.stat().st_size < 50_000:
            content = full_path.read_text()
            context_parts.append(f"### {filepath}\n```\n{content}\n```")
            total_tokens += len(content) // 4
            if total_tokens > 20_000:  # Token budget cap
                break

    return "\n\n".join(context_parts)
```

**B. Replace Append with Unified Diff Strategy**

Change the API prompt from "give me new code to append" to "give me a unified diff":

```
For each file change, provide a unified diff format:
--- a/path/to/file.php
+++ b/path/to/file.php
@@ -10,3 +10,5 @@
 existing line
+new line added
 existing line
```

Then apply changes using Python's `difflib` or `patch` — precise insertion, not append.

**C. Full File Replace When Under Token Budget**

For files under 500 lines — have the AI return the complete file content with changes applied (current `merge_strategy=replace`). Only fall back to diff for large files.

**Implementation:**
- New method `_get_targeted_context()` in `implement.py` (~50 lines)
- Two-pass AI call: file selection (cheap) → implementation with context (full)
- New `merge_strategy=diff` support in `plan.py` `apply_plan()` (~40 lines)
- Update prompt template to request full file or diff based on size
- Estimated effort: ~150 lines total

---

## Issue #4: Only Drupal Actually Works

### Root Cause
`test_runner.py` only has 4 PHP handler methods. The 6 profiles in `profiles.py` define 15+ check names that silently resolve to `None`. Scoped file extensions are PHP-only.

### Approach

**A. Implement All Profile Test Handlers**

Add the missing handlers — each is ~10 lines (same pattern as PHP handlers):

```python
# Python
def _run_pytest(self, scoped_files): ...     # pytest {module_path} or specific files
def _run_ruff(self, scoped_files): ...       # ruff check {files}
def _run_mypy(self, scoped_files): ...       # mypy {files}

# React / Angular
def _run_jest(self, scoped_files): ...       # npx jest --passWithNoTests
def _run_eslint(self, scoped_files): ...     # npx eslint {files}
def _run_tsc(self, scoped_files): ...        # npx tsc --noEmit
def _run_ng_test(self, scoped_files): ...    # npx ng test --watch=false
def _run_ng_lint(self, scoped_files): ...    # npx ng lint

# Java
def _run_mvn_test(self, scoped_files): ...   # mvn test -pl {module}
def _run_checkstyle(self, scoped_files): ... # mvn checkstyle:check
def _run_spotbugs(self, scoped_files): ...   # mvn spotbugs:check

# .NET
def _run_dotnet_test(self, scoped_files): ...   # dotnet test
def _run_dotnet_format(self, scoped_files): ... # dotnet format --verify-no-changes
def _run_dotnet_build(self, scoped_files): ...  # dotnet build /warnaserror
```

**B. Profile-Driven Scoped Extensions**

Replace the hardcoded extension filter with profile-driven extensions:

```python
def _get_scoped_files(self, changed_files):
    # Pull extensions from active profile instead of hardcoding
    from src.profiles import get_profile
    profile = get_profile(self.config)
    lint_extensions = set(profile.get("file_extensions", []))
    # ... rest of filtering logic
```

**C. Container-Aware Command Building**

Not all frameworks use DDEV. Add a `command_style` to profiles:

```python
# Drupal: "ddev exec phpunit ..."
# Python: "pytest ..." (native, no container)
# React: "npx jest ..." (native)
# Java: "mvn test" (native)
```

Let profiles define whether they need `container_cmd` prefix or run natively.

**Implementation:**
- ~15 new handler methods in `test_runner.py` (~150 lines total, each ~10 lines)
- Update `_get_scoped_files()` to use profile extensions (~10 lines)
- Add `container_required: bool` to each profile (~6 lines in `profiles.py`)
- Update `_exec()` to conditionally prefix container command (~10 lines)
- Estimated effort: ~180 lines

---

## Issue #5: No Feedback Loop — It Fails and Stops

### Root Cause
Pipeline runs test → logs failure → continues to review → never feeds errors back to AI for fix attempt.

### Approach

**A. Iterative Fix Loop (Max 3 Attempts)**

After test failure, feed the errors back to the AI and ask for fixes:

```
Pipeline Flow (new):
  Implement → Test
    ↓ (pass) → Review → Push
    ↓ (fail) → Feed errors to AI → Re-implement → Re-test (attempt 2)
    ↓ (fail) → Feed errors to AI → Re-implement → Re-test (attempt 3)
    ↓ (fail) → Stop, report all attempts in ADO comment
```

**Implementation sketch in `pipeline.py`:**

```python
max_fix_attempts = self.config.get("ai_agent", {}).get("max_fix_attempts", 3)

for attempt in range(1, max_fix_attempts + 1):
    test_summary = self.test_runner.run_all(changed_files=changed)

    if test_summary.all_passed:
        break

    if attempt < max_fix_attempts:
        # Build fix prompt with test errors
        fix_context = (
            f"## Test Failures (Attempt {attempt}/{max_fix_attempts})\n\n"
            f"{test_summary.summary_text()}\n\n"
            f"Fix the code to resolve these failures. "
            f"Do not re-introduce previously fixed issues."
        )
        # Re-run implementation with error context
        fix_result = self.implementer.implement(
            story_context=full_context + "\n\n" + fix_context,
            config=self.config,
        )
        # Re-commit
        changed = self.git.get_changed_files()
        if changed:
            self.git.commit_changes(work_item.id, f"Fix attempt {attempt}")
```

**B. Auto-Fix for Lint Errors**

For deterministic lint failures (phpcs, ruff, eslint), run auto-fix tools before involving AI:

```python
def _auto_fix_lint(self, tool: str) -> bool:
    """Try auto-fixing lint errors with the tool's built-in fixer."""
    fixers = {
        "phpcs": [self.container_cmd, "exec", "phpcbf", "--standard=Drupal", self.module_path],
        "ruff": ["ruff", "check", "--fix", self.module_path],
        "eslint": ["npx", "eslint", "--fix", self.module_path],
        "dotnet_format": ["dotnet", "format", self.module_path],
    }
    if tool in fixers:
        result = subprocess.run(fixers[tool], cwd=self.workspace_dir, capture_output=True)
        return result.returncode == 0
    return False
```

**Implementation:**
- Add `max_fix_attempts` config (default: 3)
- Wrap test stage in retry loop (~40 lines in `pipeline.py`)
- Add `_auto_fix_lint()` to `test_runner.py` (~25 lines)
- Pass test errors as additional context to `implement()` (~10 lines)
- Track attempts in SSE events and ADO comments
- Estimated effort: ~100 lines

---

## Issue #6: PR Creation Only Works with GitHub

### Root Cause
`create_pull_request()` hardcodes `gh pr create`. No Azure DevOps or GitLab PR support.

### Approach

**A. Multi-Platform PR Creation**

Detect the git hosting platform from remote URL and use the appropriate tool:

```python
def create_pull_request(self, work_item_id, title, description, branch_name):
    platform = self._detect_platform()

    if platform == "github":
        return self._create_github_pr(...)
    elif platform == "azure_devops":
        return self._create_ado_pr(...)
    elif platform == "gitlab":
        return self._create_gitlab_pr(...)
    else:
        return {"success": False, "error": f"PR creation not supported for {platform}"}

def _detect_platform(self) -> str:
    remote_url = self._run("remote", "get-url", "origin").strip()
    if "github.com" in remote_url:
        return "github"
    elif "dev.azure.com" in remote_url or "visualstudio.com" in remote_url:
        return "azure_devops"
    elif "gitlab" in remote_url:
        return "gitlab"
    return "unknown"

def _create_ado_pr(self, work_item_id, title, description, branch_name, target):
    """Create PR using Azure DevOps CLI."""
    cmd = [
        "az", "repos", "pr", "create",
        "--title", f"#{work_item_id} - {title}",
        "--description", description[:4000],
        "--source-branch", branch_name,
        "--target-branch", target,
        "--work-items", str(work_item_id),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Parse JSON output for PR URL
    ...
```

**B. Config Override**

Allow explicit config override for teams with non-standard setups:

```yaml
git:
  pr_platform: "azure_devops"  # Override auto-detection: github, azure_devops, gitlab
```

**Implementation:**
- Add `_detect_platform()` (~15 lines)
- Add `_create_ado_pr()` using `az repos pr create` (~30 lines)
- Add `_create_gitlab_pr()` using `glab` or GitLab API (~30 lines)
- Refactor `create_pull_request()` to dispatch (~10 lines)
- Add `git.pr_platform` config option
- Estimated effort: ~100 lines

---

## Issue #7: Dashboard Is a Viewer, Not a Workspace

### Root Cause
No edit capability, no rejection feedback, no inline diff view. Plan modal shows file list but code is read-only.

### Approach

**A. Inline Diff View with Syntax Highlighting**

Replace the plain "View" button with a side-by-side or unified diff view:

- Use **Monaco Editor** (same engine as VS Code) or **CodeMirror** as embedded editor in the plan modal
- Show original file on left, proposed changes on right
- Syntax highlighting by file extension (PHP, Python, JS, etc.)

**B. Editable Plan Content**

Allow users to modify the AI's proposed code before approving:

```
New API endpoint: POST /api/plan/file-content/update
Body: { "file_index": 0, "content": "corrected code here" }
```

Pipeline applies the user-edited content instead of the AI's original. This makes the dashboard a true code review tool.

**C. Rejection Feedback**

When rejecting a plan (or individual files), prompt for a reason:

```
? Why are you rejecting this? [optional]
> "Don't modify the hook, add a new service class instead"
```

This feedback is:
1. Stored in the run record
2. Injected into the AI prompt on retry: *"Previous approach was rejected. User feedback: 'Don't modify the hook...'"*
3. Builds a learning corpus over time

**D. Implementation Approach — Phased**

**Phase 1 (Quick Win):** Add diff endpoint that returns original vs proposed for each file. Frontend renders with a lightweight diff library (e.g., `diff2html.js`). Read-only but visual.

**Phase 2:** Integrate CodeMirror in plan modal. Allow edits. Add `/api/plan/file-content/update` endpoint.

**Phase 3:** Add rejection feedback textarea. Store in run history. Inject on retry.

**Implementation:**
- Phase 1: New `/api/plan/file-diff/<index>` endpoint (~30 lines), frontend diff rendering (~50 lines JS)
- Phase 2: CodeMirror integration (~100 lines JS), edit endpoint (~20 lines Python)
- Phase 3: Rejection feedback (~30 lines Python, ~20 lines JS)
- Estimated effort: ~250 lines total across phases

---

## Issue #8: Single-User, No Team Value

### Root Cause
Global mutable state, no auth, no persistent storage, no multi-user support.

### Approach

**A. Accept the Single-User Reality (Short Term)**

For v1, **own it**: this is a personal developer tool. Don't pretend it's a team platform. Remove the complexity that implies otherwise. Instead, focus on making the single-user experience flawless.

Add to README: *"DevOps AI Agent is a personal automation tool. Each developer runs their own instance against their own workspace."*

**B. Run-Level Isolation (Medium Term)**

Replace global state with per-run state keyed by `run_id`:

```python
# Instead of:
_consent_state = {"pending": False, ...}

# Use:
_run_states: dict[str, RunState] = {}

@dataclass
class RunState:
    run_id: str
    consent: ConsentState
    plan: PlanState
    push: PushState
```

This enables the dashboard to show **multiple concurrent pipeline runs** (e.g., one per story in queue mode) without state conflicts.

**C. Persistent Run History (Medium Term)**

Replace the 10-entry JSON file with SQLite:

```python
# .dai/history.db
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    work_item_id INTEGER,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,  -- success / failed / cancelled
    failed_stage TEXT,
    method TEXT,
    changed_files TEXT,  -- JSON array
    test_results TEXT,   -- JSON
    review_verdict TEXT,
    ai_output TEXT,
    error TEXT
);
```

Benefits:
- Unlimited history with querying
- Team lead can review what the AI did across stories
- Retry logic can query all past attempts, not just last 10

**D. CI/CD Integration (Long Term)**

Provide a GitHub Actions / Azure Pipelines YAML that runs `dai run-all` on a schedule:

```yaml
# .github/workflows/dai-nightly.yml
name: DevOps AI Agent - Nightly
on:
  schedule:
    - cron: '0 2 * * *'  # 2 AM daily
jobs:
  process-stories:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install devops-ai-agent
      - run: dai run-all --approval-mode auto --skip-push
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          AZURE_DEVOPS_PAT: ${{ secrets.ADO_PAT }}
```

This shifts the tool from "personal CLI" to "team CI pipeline" without changing core architecture.

**Implementation:**
- Per-run state isolation: ~60 lines refactor in `app.py`
- SQLite history: ~100 lines new `src/utils/history.py` + migration from JSON
- CI/CD template: ~30 lines YAML template
- Estimated effort: ~200 lines

---

## Issue #9: Constant Babysitting (3 Manual Gates)

### Root Cause
Data consent, plan approval, and push confirmation all block the pipeline waiting for user input. Even in "auto" mode, consent still fires.

### Approach

**A. Trust Levels — Let the User Choose Their Comfort**

Introduce a `trust_level` config that controls how many gates are active:

```yaml
ai_agent:
  # Trust levels:
  #   "cautious"    — all gates: consent + plan approval + push confirmation (current default)
  #   "balanced"    — skip consent, keep plan + push approval
  #   "autonomous"  — skip consent + plan, keep push confirmation only
  #   "full-auto"   — no gates, fully unattended (for CI/CD or overnight batch)
  trust_level: "balanced"
```

**Implementation mapping:**

| Gate | cautious | balanced | autonomous | full-auto |
|------|----------|----------|-----------|-----------|
| Data consent | ✓ | ✗ | ✗ | ✗ |
| Plan approval | ✓ | ✓ | ✗ | ✗ |
| Push confirmation | ✓ | ✓ | ✓ | ✗ |
| Auto-PR | config | config | config | ✓ |

**B. Overnight Batch Mode**

`dai run-all --trust full-auto` processes all tagged stories unattended. Morning report:

```
DevOps AI Agent — Overnight Run Summary (March 27, 2026)
═══════════════════════════════════════════════════════════
Processed: 8 stories
  ✓ Succeeded: 5 (branches ready for review)
  ✗ Failed: 2 (test failures after 3 fix attempts)
  ⚠ Skipped: 1 (story quality too low)

Successful:
  #1234 - Add translation config form    → feature/1234-add-translation  ✓ tests pass
  #1235 - Fix label display issue        → feature/1235-fix-label        ✓ tests pass
  ...

Failed:
  #1236 - Refactor queue processor       → 3 attempts, phpunit failure (see ADO comment)

Review these branches and merge when ready.
```

**C. Remember Consent Per-Provider**

Don't ask consent every run if the user already approved sending data to the same provider:

```python
# .dai/consent.json
{"copilot": {"approved": true, "approved_at": "2026-03-27T10:00:00Z"}}
```

Skip consent prompt if provider was previously approved. Offer `dai consent --reset` to revoke.

**Implementation:**
- Add `trust_level` config + gate-skipping logic (~40 lines in `pipeline.py`)
- Add consent persistence (~20 lines)
- Add overnight summary report generator (~50 lines)
- Estimated effort: ~110 lines

---

## Issue #10: Value Proposition — What Problem Does This Solve?

### Reframing the Narrative

Stop positioning as "Copilot alternative." Position as:

> **"The tool that turns your sprint backlog into working branches while you sleep."**

### The Value Matrix We Should Communicate

| Capability | Copilot Can | We Can | Why It Matters |
|---|---|---|---|
| Process 10 stories overnight | ✗ | ✓ | Team velocity, not individual speed |
| ADO story → branch → test → PR | ✗ | ✓ | Full lifecycle automation |
| Post AI analysis back to ADO | ✗ | ✓ | PM visibility, story quality feedback |
| Enforce coding standards (phpcs/ruff) | ✗ | ✓ | Consistency without human review |
| Auto-retry on test failure | ✗ | ✓ (after fix #5) | Self-correcting pipeline |
| Audit trail per story | ✗ | ✓ (after fix #8) | Compliance, team visibility |
| Zendesk → ADO → Code → PR | ✗ | ✓ | Support ticket → fix pipeline |
| Run headless in CI/CD | ✗ | ✓ (after fix #9) | Scale across projects |

### Kill Features to Build

1. **Morning Report Email/Slack** — "Here's what the AI did overnight, review these 5 branches"
2. **Story Dashboard** — not pipeline monitoring, but **sprint progress**: which stories are AI-ready, which are in progress, which need human review
3. **Correction Learning** — every time a developer modifies AI output before merging, capture the delta. Over time, the system learns project-specific patterns.

---

## Critic Follow-Up: "Focus on Core Engine, Not UI"

> *"Rather than focusing on UI you should focus on more concrete flow and actual coding automation."*

**Valid.** The dashboard is chrome on a weak engine. We spent effort on SSE progress bars, approval modals, and push confirmation dialogs while the core coding pipeline has fundamental gaps:

- AI can't read files it's modifying (API mode)
- Tests fail → pipeline gives up (no self-correction)
- Only 1 of 6 claimed frameworks actually works
- No learning from past runs or corrections

**The dashboard is a monitoring layer. The engine is the product.** If the engine produces working code on 8/10 stories, nobody cares if the dashboard is ugly. If the engine fails on 8/10 stories, nobody cares if the dashboard is beautiful.

### Revised Priority: Engine-First Roadmap

Dashboard work is now deferred to the final phase. All investment goes into making the coding pipeline reliable, self-correcting, and context-aware.

---

## Implementation Roadmap (Revised — Engine First)

### Phase 1: Core Engine — Self-Correcting Pipeline (Week 1-2)
**Goal:** The pipeline can implement a story, hit test failures, fix its own mistakes, and produce passing code — without human intervention.

| # | Fix | Impact | Effort | Critic Issue |
|---|-----|--------|--------|---|
| 1 | **Iterative fix loop (3 attempts)** — feed test errors back to AI, re-implement, re-test | Goes from "fail and stop" to "fail, learn, fix" | ~100 lines | #5 |
| 2 | **Auto-fix lint errors** — run `phpcbf`/`ruff --fix`/`eslint --fix` before involving AI | Eliminates 50%+ of lint failures instantly | ~30 lines | #5 |
| 3 | **Trust levels** — `cautious/balanced/autonomous/full-auto` config for gate control | Enables unattended batch processing | ~110 lines | #9 |
| 4 | **Consent persistence** — remember approval per AI provider, don't re-ask | Removes the most annoying gate | ~20 lines | #9 |

**Validation:** Run 5 Drupal stories end-to-end in `full-auto` mode. Measure: how many produce branches with passing tests?

### Phase 2: Core Engine — Context-Aware Implementation (Week 2-3)
**Goal:** AI receives enough codebase context to produce correct code on first attempt, even without CLI filesystem access.

| # | Fix | Impact | Effort | Critic Issue |
|---|-----|--------|--------|---|
| 5 | **Smart context injection** — two-pass AI call: file selection → read files → implement with context | AI sees actual code it's modifying, not just filenames | ~100 lines | #3 |
| 6 | **Diff-based merge strategy** — replace naive "append to end of file" with unified diff application | Code lands in the right place (inside classes, after imports) | ~50 lines | #3 |
| 7 | **Full-file replace for small files** — files <500 lines get `merge_strategy=replace` automatically | Most module files are small; complete file output is more reliable | ~20 lines | #3 |
| 8 | **Story quality scoring** — analyze story before implementing; gate on quality threshold | Prevents wasted AI calls on vague "fix the thing" stories | ~80 lines | #2 |

**Validation:** Compare implementation accuracy: API mode with context vs without. Target: API mode produces correct output on 60%+ of stories (vs ~20% today).

### Phase 3: Core Engine — Multi-Framework & Multi-Platform (Week 3-4)
**Goal:** The tool actually works for Python, React, and Java — not just Drupal. PRs work with Azure DevOps repos, not just GitHub.

| # | Fix | Impact | Effort | Critic Issue |
|---|-----|--------|--------|---|
| 9 | **All test runner handlers** — `_run_pytest`, `_run_ruff`, `_run_mypy`, `_run_jest`, `_run_eslint`, `_run_tsc`, `_run_mvn_test`, `_run_dotnet_test`, etc. | 6 claimed frameworks → 6 working frameworks | ~180 lines | #4 |
| 10 | **Profile-driven scoped extensions** — pull lintable file extensions from profile, not hardcoded PHP | Scoped testing works for `.py`, `.ts`, `.java`, `.cs` | ~20 lines | #4 |
| 11 | **Container-aware command building** — profiles declare whether they need DDEV/Docker prefix or run natively | Python/React/Java don't use DDEV | ~15 lines | #4 |
| 12 | **Multi-platform PR creation** — detect GitHub/ADO/GitLab from remote URL, use appropriate CLI | `az repos pr create` for ADO repos, `gh pr create` for GitHub | ~100 lines | #6 |

**Validation:** Run a Python project story end-to-end: `dai run` → branch → implement → pytest + ruff pass → review → PR.

### Phase 4: Core Engine — Batch Automation & Learning (Week 4-6)
**Goal:** Process 10 stories overnight unattended. Learn from past runs.

| # | Fix | Impact | Effort | Critic Issue |
|---|-----|--------|--------|---|
| 13 | **Overnight batch mode** — `dai run-all --trust full-auto` with morning summary report | The killer differentiator vs Copilot | ~150 lines | #9, #10 |
| 14 | **SQLite run history** — replace 10-entry JSON with queryable database | Unlimited history, team audit trail, retry intelligence | ~100 lines | #8 |
| 15 | **History-aware retry** — inject all past failure attempts into AI context on retry | AI doesn't repeat the same mistakes | ~30 lines | #5 |
| 16 | **Rejection feedback storage** — when user modifies AI output before merge, capture the delta | Build corpus of "what AI got wrong" per project | ~50 lines | #7 |
| 17 | **CI/CD template** — GitHub Actions / Azure Pipelines YAML for nightly `dai run-all` | Scale from personal CLI to team pipeline | ~30 lines | #8 |

**Validation:** 8-story overnight run. Target: 5+ branches with passing tests by morning.

### Phase 5: Onboarding & Setup (Week 6-7)
**Goal:** New users can go from install to first successful run in under 5 minutes.

| # | Fix | Impact | Effort | Critic Issue |
|---|-----|--------|--------|---|
| 18 | **`dai init`** — interactive setup wizard, auto-detects workspace/org/auth | One-time setup, never touch YAML manually | ~150 lines | #1 |
| 19 | **`dai doctor`** — environment health check with actionable fixes | Self-service troubleshooting | ~100 lines | #1 |

**Validation:** Hand tool to a new developer. Time from `pip install` to first `dai fetch` succeeds: <5 minutes.

### Phase 6: Dashboard (Deferred — Only After Engine Is Solid)
**Goal:** Improve the monitoring/approval experience — but only after the core pipeline reliably produces good code.

| # | Fix | Impact | Effort | Critic Issue |
|---|-----|--------|--------|---|
| 20 | Read-only diff view in plan modal | Better code review experience | ~80 lines | #7 |
| 21 | Editable plan content (CodeMirror) | Dashboard becomes a workspace | ~150 lines | #7 |
| 22 | Per-run state isolation | Support concurrent pipeline runs | ~60 lines | #8 |

**Gating criteria:** Dashboard work only begins after Phase 1-4 validation shows the engine can process 5+ stories overnight with passing tests.

---

## What We Explicitly Won't Do

1. **Build a VS Code extension** — Copilot owns the in-editor space. We're a headless pipeline.
2. **Invest in dashboard before engine works** — no Monaco editors, no sprint dashboards, no rich UI until the core reliably produces working code.
3. **Multi-user auth** — each dev runs their own instance. Team value comes from CI/CD integration and ADO comments.
4. **Support every git host** — GitHub + Azure DevOps. GitLab is nice-to-have later.
5. **Build real-time pair programming** — that's not our product. We're batch automation.

---

## Success Metrics (Engine-Focused)

| Metric | Current State | Phase 1-2 Target | Phase 3-4 Target |
|--------|--------------|------------------|------------------|
| Stories producing passing code (Drupal) | ~20% | 50% | 70% |
| Self-correction rate (fix on retry) | 0% | 40% | 60% |
| API mode accuracy (without CLI tools) | ~15% | 45% | 60% |
| Frameworks with working test runners | 1/6 | 1/6 | 4/6 |
| Unattended stories per night | 0 | 3 | 8 |
| Time from install to first run | ~30 min | ~30 min | <5 min |

**The pitch stays the same, but it's backed by a real engine now:**

*"I set it up on Friday. Monday morning I had 5 branches with passing tests waiting for my review."*

The dashboard is just how you watch it happen. The engine is what makes it happen.

---

## Impact Analysis: With vs Without Claude Code API

> **Added:** March 30, 2026
> **Context:** Evaluating how access to Claude Code API changes our architecture, roadmap, and success probability.

### The Fundamental Shift

```
WITHOUT Claude Code API:
  Our tool = Orchestration + AI Brain + Test Runner + Git Automation
  (We're responsible for making the AI produce correct code → HARD)

WITH Claude Code API:
  Our tool = Orchestration + Test Runner + Git Automation
  Claude Code = AI Brain
  (We orchestrate; Claude Code produces correct code → EASY)
```

Without Claude Code, we are building an AI coding engine AND a DevOps pipeline. With Claude Code, we **only build the DevOps pipeline** — Claude Code is the coding engine. This is a dramatically simpler and more defensible product.

---

### What Claude Code API Gives Us Natively

| Capability | We Were Building (Phase) | Effort | Claude Code Does It |
|---|---|---|---|
| Read existing files before modifying | Phase 2: Smart context injection | ~100 lines | **Native** — reads files from disk |
| Correct merge (inside classes, after imports) | Phase 2: Diff-based merge strategy | ~50 lines | **Native** — `merge_strategy=replace`, full file output |
| Understand project structure | Phase 2: Two-pass file selection | ~50 lines | **Native** — traverses filesystem |
| Handle complex multi-file changes | Not planned — too hard for API mode | N/A | **Native** — agentic, explores dependencies |
| Fix its own test failures internally | Phase 1: Iterative fix loop | ~100 lines | **Partially** — has internal iteration, but we still need the outer test→fix cycle |

---

### Phase-by-Phase Impact

#### Phase 1: Self-Correcting Pipeline — SIMPLIFIED (~30% less effort)

| Deliverable | Without Claude Code | With Claude Code | Verdict |
|---|---|---|---|
| Iterative fix loop (test→AI→fix→retest) | **Must build** (~100 lines) — AI is blind, needs error context injected manually, prompt engineering for fix strategies | **Simpler** (~40 lines) — Claude Code handles internal file reading and fix logic; we just orchestrate the outer loop: run tests → feed failure output → re-invoke | **Keep, but simpler** |
| Auto-fix lint (phpcbf/ruff --fix) | **Must build** (~30 lines) | **Still valuable** — deterministic fixers are faster/cheaper than AI tokens | **Keep** |
| Trust levels (cautious→full-auto) | **Must build** (~110 lines) | **Still needed** — controls consent/push gates | **Keep** |
| Consent persistence | **Must build** (~20 lines) | **Still needed** | **Keep** |
| Story quality scoring | Phase 2 (~80 lines) | **Still needed** — garbage in = garbage out regardless of AI capability. Moves up to Phase 1 | **Keep, move earlier** |

#### Phase 2: Context-Aware Implementation — ELIMINATED ENTIRELY

| Deliverable | Without Claude Code | With Claude Code | Verdict |
|---|---|---|---|
| Smart context injection (two-pass AI: file selection → read files → implement) | **Must build** (~100 lines) — most complex code in the pipeline | Claude Code reads files natively from the workspace | **DROP** |
| Diff-based merge strategy (replace naive append) | **Must build** (~50 lines) — fragile, AST-unaware | Claude Code returns complete files with changes applied correctly | **DROP** |
| Full-file replace for small files | **Must build** (~20 lines) | Already the default behavior with Claude Code | **DROP** |

**~170 lines of the hardest, most error-prone code eliminated.**

#### Phase 3: Multi-Framework & Multi-Platform — UNCHANGED

| Deliverable | Without Claude Code | With Claude Code | Verdict |
|---|---|---|---|
| All test runner handlers | Claude Code doesn't run test suites — we do | Same | **Keep** |
| Profile-driven scoped extensions | Still needed for test scoping | Same | **Keep** |
| Container-aware command building | Still needed | Same | **Keep** |
| Multi-platform PR creation | Git hosting ≠ AI model | Same | **Keep** |

**These are infrastructure problems, not AI problems. No change.**

#### Phase 4: Batch Automation & Learning — ACCELERATED

| Deliverable | Without Claude Code | With Claude Code | Verdict |
|---|---|---|---|
| Overnight batch mode | **High risk** — API mode produces broken code ~80% of the time, so overnight batch is unreliable | **Low risk** — Claude Code success rate is 70-90%, making overnight batch the headline feature | **Keep — now actually viable** |
| SQLite run history | Same need | Same need | **Keep** |
| History-aware retry | **Critical** — AI keeps repeating same mistakes without past context | **Still useful** — but fewer retries needed because Claude Code is smarter | **Keep, lower priority** |
| Rejection feedback storage | Same need | Same need | **Keep** |
| CI/CD template | Same need | Same need | **Keep** |

#### Phase 5: Onboarding — UNCHANGED

`dai init` and `dai doctor` are needed regardless of AI model.

#### Phase 6: Dashboard — EVEN FURTHER DEPRIORITIZED

| Deliverable | Without Claude Code | With Claude Code | Verdict |
|---|---|---|---|
| Read-only diff view | Needed to review bad AI output | Less critical — output quality is much higher | **Defer further** |
| Editable plan content (CodeMirror) | Needed because AI output requires manual fixes frequently | **Much less needed** — Claude Code output is usually correct | **Defer or drop** |
| Per-run state isolation | Needed for concurrent runs | Same | **Keep for later** |

**If Claude Code produces correct code 80% of the time, you don't need an in-browser code editor to fix AI mistakes.**

---

### Revised Roadmap (With Claude Code API)

#### Phase 1: Orchestration + Self-Correction (Week 1-2)
The pipeline becomes a **thin orchestration layer** around Claude Code.

| # | Deliverable | Effort | Notes |
|---|---|---|---|
| 1 | **Claude Code API integration** — replace CLI subprocess with direct API calls | ~80 lines | New `_call_claude_code_api()` in implement.py |
| 2 | **Trust levels** (cautious→full-auto) | ~110 lines | Same as before |
| 3 | **Outer fix loop** — run tests → feed errors to Claude Code → re-invoke → retest (max 3) | ~40 lines | Simpler — Claude Code handles internal iteration |
| 4 | **Auto-fix lint** — phpcbf/ruff/eslint before AI retry | ~30 lines | Same as before |
| 5 | **Story quality scoring** — gate on quality before expensive Claude Code call | ~80 lines | Moved from old Phase 2 |
| 6 | **Consent persistence** | ~20 lines | Same as before |

#### Phase 2: Multi-Framework & Multi-Platform (Week 2-3)
Unchanged — infrastructure problems.

| # | Deliverable | Effort |
|---|---|---|
| 7 | All test runner handlers (pytest, jest, mvn, dotnet, etc.) | ~180 lines |
| 8 | Profile-driven scoped extensions | ~20 lines |
| 9 | Container-aware command building | ~15 lines |
| 10 | Multi-platform PR creation (GitHub + ADO) | ~100 lines |

#### Phase 3: Batch Automation + History (Week 3-5)
Now the **headline feature** — viable because Claude Code actually works.

| # | Deliverable | Effort |
|---|---|---|
| 11 | **Overnight batch mode** + morning summary report | ~150 lines |
| 12 | SQLite run history | ~100 lines |
| 13 | History-aware retry context | ~30 lines |
| 14 | CI/CD template (GitHub Actions / Azure Pipelines) | ~30 lines |
| 15 | Rejection feedback storage | ~50 lines |

#### Phase 4: Onboarding (Week 5-6)

| # | Deliverable | Effort |
|---|---|---|
| 16 | `dai init` — interactive setup wizard | ~150 lines |
| 17 | `dai doctor` — environment health check | ~100 lines |

#### Phase 5: Dashboard (Only if needed — Gated)
**Gated:** Only start if users report they need it. With Claude Code's quality, most users may just use `dai run --trust full-auto` and review branches in their IDE.

---

### Side-by-Side Comparison: Effort & Outcomes

| Metric | Without Claude Code | With Claude Code | Delta |
|--------|--------------------|--------------------|-------|
| **Total phases** | 6 | 5 | -1 phase |
| **Lines of hardest code** (context injection, diff merge) | ~220 lines | 0 lines | **Eliminated** |
| **Total estimated effort** | ~1,900 lines | ~1,165 lines | **-39%** |
| **Expected overnight success rate** | ~20-30% | ~70-80% | **3-4x improvement** |
| **Self-correction success** | ~30% (AI is blind to code) | ~60% (AI reads files natively) | **2x improvement** |
| **Time to "5 stories overnight"** | Week 6+ (needs Phase 1-4) | Week 3-4 (Phase 1-3 sufficient) | **~50% faster** |
| **Dashboard necessity** | High (need to fix AI output) | Low (output is usually correct) | **Deprioritized** |
| **Product complexity** | High (we build AI brain + DevOps pipeline) | Low (we build DevOps pipeline only) | **Dramatically simpler** |

---

### Strategic Implication

Without Claude Code, we are building **two hard things**: an AI coding engine (context injection, diff merging, fix loops) AND a DevOps automation pipeline. Most of our effort goes into making a mediocre AI work adequately.

With Claude Code, we build **one thing well**: a DevOps automation pipeline that uses a world-class AI coding engine. Our product identity clarifies:

> **We're not an AI coding tool. We're a DevOps automation layer that uses Claude Code as its coding engine.**

This is a much stronger, more defensible, and more honest position. We stop competing with Copilot/Cursor on "who produces better code" and instead compete on "who automates the full DevOps lifecycle end-to-end."

**The pitch becomes irrefutable:**

*"Claude Code writes the code. We handle everything else — stories, branches, tests, reviews, PRs, state transitions — while you sleep."*

---

## Clarification: Claude Opus 4.6 API vs Claude Code API

> **Added:** March 30, 2026
> **Context:** Important distinction — upgrading the AI model is NOT the same as gaining agentic filesystem access.

### They Are Different Products

| | Claude Opus 4.6 (Standard API) | Claude Code API |
|---|---|---|
| **What it is** | Chat/completion API — send prompt, get text back | Agentic API — AI reads files, writes files, runs commands autonomously |
| **Filesystem access** | **None** — AI only sees what we put in the prompt | **Full** — AI reads/writes files directly in the workspace |
| **Current status in our tool** | Available now — `_call_anthropic()` in implement.py | Not yet integrated |
| **Merge strategy needed** | Still append or manually-built context injection | Replace — AI returns complete correct files |
| **Multi-file understanding** | Only sees files we explicitly include in prompt | Explores dependencies, imports, related files on its own |
| **Self-correction** | Cannot — doesn't see what it broke | Can — reads its own output, verifies, fixes |
| **Cost** | Per-token (input + output) | Per-token (higher, but fewer calls needed) |

### What Opus 4.6 Standard API Improves Over GPT-4o

| Aspect | GPT-4o (current copilot provider) | Claude Opus 4.6 (standard API) | Real Impact |
|---|---|---|---|
| Reasoning quality | Good | Excellent — deeper logical chains | Better code logic, fewer bugs |
| Instruction following | Sometimes ignores constraints | Very precise, follows format specs | Cleaner JSON plans, correct structure |
| Context window | 128K tokens | 200K tokens | Can receive more file contents in prompt |
| Code accuracy | ~60-70% for simple changes | ~80-85% for simple changes | Fewer fix attempts needed |
| Filesystem access | **None** | **Still none** | No change — still needs our context injection |
| Phase 2 (context injection) needed? | Yes | **Still yes** | Our hardest code is still required |

### The Key Point

```
Claude Opus 4.6 Standard API = Better brain, same blindness
Claude Code API              = Better brain + can see and touch files
```

Opus 4.6 via standard API is a **meaningful upgrade in output quality** — roughly 20-30% better code accuracy than GPT-4o. But it **does not eliminate Phase 2** (smart context injection, diff-based merge). The AI still only knows what we explicitly include in the prompt. It cannot read a file to understand existing code structure before modifying it.

### Three-Way Roadmap Impact

| Phase | GPT-4o (current) | Claude Opus 4.6 API | Claude Code API |
|---|---|---|---|
| Phase 1: Self-correction | Must build (full) | Must build (full) | Simplified (~30% less) |
| Phase 2: Context injection | **Must build (full)** | **Must build (full)** | **Eliminated entirely** |
| Phase 3: Multi-framework | Must build | Must build | Must build |
| Phase 4: Batch automation | Risky (~20% success) | Moderate (~40-50% success) | Viable (~70-80% success) |
| Phase 5: Onboarding | Must build | Must build | Must build |
| Phase 6: Dashboard | High need (fix bad output) | Medium need | Low need |
| **Total effort** | ~1,900 lines | ~1,900 lines (same code, better results) | ~1,165 lines (-39%) |
| **Overnight success rate** | ~20% | **~40-50%** | **~70-80%** |
| **Time to reliable batch** | Week 6+ | Week 5+ | Week 3-4 |

### Recommendation

**Immediate:** Switch from GPT-4o (copilot provider) to Claude Opus 4.6 (anthropic provider) as the default AI model. This is a config change — no code changes required. Immediate quality improvement.

**Short-term:** Still build the full "Without Claude Code" roadmap (all 6 phases). Opus 4.6 makes output better but doesn't change what we need to build.

**When Claude Code API becomes available:** Refactor to the simplified "With Claude Code" roadmap. Drop Phase 2 entirely, simplify Phase 1, and promote overnight batch to the headline feature.

### Summary: Three Tiers of Capability

```
Tier 1 (NOW):     GPT-4o via GitHub Models API
                   → Works, but accuracy ~20% overnight
                   → Needs all 6 phases

Tier 2 (UPGRADE):  Claude Opus 4.6 via Anthropic API
                   → Better quality, accuracy ~40-50% overnight
                   → Still needs all 6 phases (same code, better results)
                   → RECOMMENDED IMMEDIATE SWITCH

Tier 3 (FUTURE):   Claude Code API
                   → Game-changer, accuracy ~70-80% overnight
                   → Eliminates Phase 2, simplifies Phase 1
                   → Build for this when available
```

**Build the roadmap for Tier 2. Design the architecture so Tier 3 is a drop-in upgrade, not a rewrite.**
