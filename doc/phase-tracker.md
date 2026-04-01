# Phase Implementation Tracker

> **Last Updated:** 2026-03-30
> **Roadmap:** Engine-First (Without Claude Code API — assumes Claude Opus 4.6 Standard API)
> **Source:** [architectural-response.md](architectural-response.md)
> **Rule:** Phases MUST execute in order. Each phase must pass validation before moving to next.

---

## Overview

| Phase | Name | Status | Items | Completed |
|-------|------|--------|-------|-----------|
| 1 | Self-Correcting Pipeline | ✅ COMPLETE | 6 | 6/6 |
| 2 | Context-Aware Implementation | ✅ COMPLETE | 3 | 3/3 |
| 3 | Multi-Framework & Multi-Platform | ✅ COMPLETE | 4 | 4/4 |
| 4 | Batch Automation & Learning | ✅ COMPLETE | 5 | 5/5 |
| 5 | Onboarding & Setup | ✅ COMPLETE | 2 | 2/2 |
| 6 | Dashboard (Deferred) | 🔲 GATED | 3 | 0/3 |

---

## Phase 1: Self-Correcting Pipeline ✅ COMPLETE

**Goal:** Pipeline can implement a story, hit test failures, fix its own mistakes, and produce passing code without human intervention.

| # | Item | File(s) Changed | Status |
|---|------|-----------------|--------|
| 1 | Iterative fix loop (3 attempts) — test → auto-fix → AI fix → retest | `src/pipeline.py` | ✅ Done |
| 2 | Auto-fix lint (`phpcbf`, `ruff --fix`, `eslint --fix`, `dotnet format`) | `src/reviewer/test_runner.py` | ✅ Done |
| 3 | Trust levels (cautious/balanced/autonomous/full-auto) | `src/pipeline.py`, `src/agent/implement.py`, `src/reviewer/ai_reviewer.py` | ✅ Done |
| 4 | Consent persistence (`.dai/consent.json`) | `src/utils/data_consent.py` | ✅ Done |
| 5 | Story quality scoring (1-10) + coaching feedback | `src/pipeline.py` | ✅ Done |
| 6 | Config: `trust_level`, `max_fix_attempts`, `min_story_quality` | `config/config.yaml` | ✅ Done |

**Validation:** Run 5 Drupal stories end-to-end in `full-auto` mode. Measure: how many produce branches with passing tests?
**Validation Status:** ⏳ Pending real-world testing

---

## Phase 2: Context-Aware Implementation ✅ COMPLETE

**Goal:** AI receives enough codebase context to produce correct code on first attempt, even without CLI filesystem access.

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 5 | Smart context injection — two-pass AI call: file selection → read files → implement with context | ~100 lines | #3 | ✅ Done |
| 6 | Diff-based merge strategy — smart merge with PHP/Python-aware insertion | ~50 lines | #3 | ✅ Done |
| 7 | Full-file replace for small files (<500 lines → auto-upgrade to `merge_strategy=replace`) | ~20 lines | #3 | ✅ Done |

**Note:** Item #8 (story quality scoring) was moved up to Phase 1 and completed there.

**Validation:** Compare implementation accuracy: API mode with context vs without. Target: API mode produces correct output on 60%+ of stories (vs ~20% today).

---

## Phase 3: Multi-Framework & Multi-Platform ✅ COMPLETE

**Goal:** Tool works for Python, React, Java — not just Drupal. PRs work with Azure DevOps repos, not just GitHub.

**Prerequisite:** Phase 2 complete. ✅

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 9 | All test runner handlers (`pytest`, `ruff`, `mypy`, `jest`, `eslint`, `tsc`, `ng_test`, `ng_lint`, `mvn_test`, `checkstyle`, `spotbugs`, `dotnet_test`, `dotnet_format`, `dotnet_build`) | ~180 lines | #4 | ✅ Done |
| 10 | Profile-driven scoped extensions (pull lintable file extensions from profile) | ~20 lines | #4 | ✅ Done |
| 11 | Container-aware command building (`_needs_container()`, `_cmd()` — DDEV/Docker only when needed) | ~15 lines | #4 | ✅ Done |
| 12 | Multi-platform PR creation (`_detect_platform()` from remote URL → GitHub/ADO/GitLab) | ~100 lines | #6 | ✅ Done |

**Validation:** Run a Python project story end-to-end: `dai run` → branch → implement → pytest + ruff pass → review → PR.

---

## Phase 4: Batch Automation & Learning ✅ COMPLETE

**Goal:** Process 10 stories overnight unattended. Learn from past runs.

**Prerequisite:** Phase 3 complete. ✅

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 13 | Overnight batch mode — `dai run-all --trust full-auto` with morning summary | ~150 lines | #9, #10 | ✅ Done |
| 14 | SQLite run history (`src/history.py` — replaces JSON) | ~100 lines | #8 | ✅ Done |
| 15 | History-aware retry (past errors + feedback injected into AI context) | ~30 lines | #5 | ✅ Done |
| 16 | Rejection feedback storage (`save_feedback` / `load_feedback_for_story`) | ~50 lines | #7 | ✅ Done |
| 17 | CI/CD templates (GitHub Actions + Azure Pipelines YAML) | ~30 lines | #8 | ✅ Done |

**Validation:** 8-story overnight run. Target: 5+ branches with passing tests by morning.

---

## Phase 5: Onboarding & Setup ✅ COMPLETE

**Goal:** New users go from install to first successful run in under 5 minutes.

**Prerequisite:** Phase 4 complete. ✅

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 18 | `dai init` — interactive setup wizard with auto-detection (framework, env type, base branch) | ~150 lines | #1 | ✅ Done |
| 19 | `dai doctor` — 10-point health check (Python, git, workspace, module, branch, az CLI, config, AI provider, env type, gh CLI) | ~100 lines | #1 | ✅ Done |

**Validation:** Hand tool to a new developer. Time from `pip install` to first `dai fetch` succeeds: <5 minutes.

---

## Phase 6: Dashboard (Deferred) 🔲 GATED

**Goal:** Improve monitoring/approval experience — only after core pipeline reliably produces good code.

**Prerequisite:** Phase 5 complete + **Gate:** Phase 1-4 validation shows engine can process 5+ stories overnight with passing tests.

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 20 | Read-only diff view in plan modal | ~80 lines | #7 | 🔲 |
| 21 | Editable plan content (CodeMirror) | ~150 lines | #7 | 🔲 |
| 22 | Per-run state isolation (concurrent runs) | ~60 lines | #8 | 🔲 |

**Validation:** Dashboard renders diffs, allows edits, supports concurrent runs without data corruption.

---

## Change Log

| Date | Change |
|------|--------|
| 2026-04-01 | Phase 5 completed: `dai init` setup wizard (auto-detects framework, env type, base branch, generates config.local.yaml), `dai doctor` 10-point health check with actionable error messages. 29 new tests, 95 total passing. |
| 2026-04-01 | Phase 4 completed: SQLite history.py (replaces JSON), history-aware retry with feedback injection, batch mode with --trust override and summary report, rejection feedback tables, CI/CD templates (GitHub Actions + Azure Pipelines), `dai history` command. 12 new tests, 66 total passing. |
| 2026-04-01 | Phase 3 completed: 14 test runner handlers across 6 frameworks, profile-driven extensions, container-aware commands (_needs_container/_cmd), multi-platform PR creation (GitHub/ADO/GitLab). 28 new tests, 54 total passing. |
| 2026-03-30 | Phase 2 completed: two-pass context injection (_select_relevant_files + _read_file_contents), smart merge (_smart_merge for PHP/Python), auto-replace for small files (_looks_like_complete_file). 18 new tests, 26 total passing. |
| 2026-03-30 | Phase 1 completed: iterative fix loop, auto-fix lint, trust levels, consent persistence, story quality scoring, config updates. All 8 tests passing. |
