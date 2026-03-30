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
| 2 | Context-Aware Implementation | 🔲 NOT STARTED | 3 | 0/3 |
| 3 | Multi-Framework & Multi-Platform | 🔲 NOT STARTED | 4 | 0/4 |
| 4 | Batch Automation & Learning | 🔲 NOT STARTED | 5 | 0/5 |
| 5 | Onboarding & Setup | 🔲 NOT STARTED | 2 | 0/2 |
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

## Phase 2: Context-Aware Implementation 🔲 NOT STARTED

**Goal:** AI receives enough codebase context to produce correct code on first attempt, even without CLI filesystem access.

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 5 | Smart context injection — two-pass AI call: file selection → read files → implement with context | ~100 lines | #3 | 🔲 |
| 6 | Diff-based merge strategy — replace naive append with unified diff application | ~50 lines | #3 | 🔲 |
| 7 | Full-file replace for small files (<500 lines → `merge_strategy=replace`) | ~20 lines | #3 | 🔲 |

**Note:** Item #8 (story quality scoring) was moved up to Phase 1 and completed there.

**Validation:** Compare implementation accuracy: API mode with context vs without. Target: API mode produces correct output on 60%+ of stories (vs ~20% today).

---

## Phase 3: Multi-Framework & Multi-Platform 🔲 NOT STARTED

**Goal:** Tool works for Python, React, Java — not just Drupal. PRs work with Azure DevOps repos, not just GitHub.

**Prerequisite:** Phase 2 complete.

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 9 | All test runner handlers (`pytest`, `ruff`, `mypy`, `jest`, `eslint`, `tsc`, `mvn test`, `dotnet test`) | ~180 lines | #4 | 🔲 |
| 10 | Profile-driven scoped extensions (pull lintable file extensions from profile) | ~20 lines | #4 | 🔲 |
| 11 | Container-aware command building (DDEV/Docker only when needed) | ~15 lines | #4 | 🔲 |
| 12 | Multi-platform PR creation (detect GitHub/ADO/GitLab from remote URL) | ~100 lines | #6 | 🔲 |

**Validation:** Run a Python project story end-to-end: `dai run` → branch → implement → pytest + ruff pass → review → PR.

---

## Phase 4: Batch Automation & Learning 🔲 NOT STARTED

**Goal:** Process 10 stories overnight unattended. Learn from past runs.

**Prerequisite:** Phase 3 complete.

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 13 | Overnight batch mode — `dai run-all --trust full-auto` with morning summary | ~150 lines | #9, #10 | 🔲 |
| 14 | SQLite run history (replace 10-entry JSON) | ~100 lines | #8 | 🔲 |
| 15 | History-aware retry (inject past failure attempts into AI context) | ~30 lines | #5 | 🔲 |
| 16 | Rejection feedback storage (capture user edit deltas) | ~50 lines | #7 | 🔲 |
| 17 | CI/CD template (GitHub Actions / Azure Pipelines YAML) | ~30 lines | #8 | 🔲 |

**Validation:** 8-story overnight run. Target: 5+ branches with passing tests by morning.

---

## Phase 5: Onboarding & Setup 🔲 NOT STARTED

**Goal:** New users go from install to first successful run in under 5 minutes.

**Prerequisite:** Phase 4 complete.

| # | Item | Effort | Critic Issue | Status |
|---|------|--------|--------------|--------|
| 18 | `dai init` — interactive setup wizard, auto-detect workspace/org/auth | ~150 lines | #1 | 🔲 |
| 19 | `dai doctor` — environment health check with actionable fixes | ~100 lines | #1 | 🔲 |

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
| 2026-03-30 | Phase 1 completed: iterative fix loop, auto-fix lint, trust levels, consent persistence, story quality scoring, config updates. All 8 tests passing. |
