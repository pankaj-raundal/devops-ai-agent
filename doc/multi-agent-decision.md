# Multi-Agent Architecture — Decision Document

> **Status:** Not needed now. Revisit when overnight batch runs hit throughput or token limits.
> **Date:** April 1, 2026

---

## Current Architecture: Single Sequential Pipeline

```
Fetch → Branch → Score → Implement (2-pass) → Test → Fix Loop → Review → Commit
         │                    │                  │        │           │
         │                    │                  │        │           │
      GitManager         AI Call #1          TestRunner  AI Call    AI Call
                         (select files)                 (fix)     (review)
                         AI Call #2
                         (implement)
```

One orchestrator (`pipeline.py`) calls different capabilities in sequence. Each AI call gets a role-specific prompt and model, but the pipeline holds all state in memory. No inter-process communication, no message passing, no artifact serialization.

---

## Why We Don't Need Agents Now

### 1. The pipeline is inherently sequential

You cannot review code that hasn't been written. You cannot test code that hasn't been committed. You cannot fix tests that haven't failed. There is no parallelism to exploit in the core workflow. Agents are most valuable when tasks can run concurrently — our tasks cannot.

### 2. State passing would add complexity with no benefit

Today the pipeline holds the `WorkItem`, file contents, test results, and diff in memory. With agents, every handoff becomes a serialization problem:

- Developer Agent finishes → must serialize: which files changed, what the diff is, what the plan was
- QA Agent receives → must deserialize, re-read files from disk, reconstruct context
- Review Agent receives → must deserialize test results AND code changes from two different sources

Every handoff is a lossy compression. Information the Developer Agent had (why it chose approach A over B) is lost unless explicitly serialized — which means more code, more failure modes, and more tokens spent on context reconstruction.

### 3. Role-based prompts already give us 80% of the benefit

The main advantage of separate agents is **specialized context and persona**. We already achieve this:

| Pipeline Stage | System Prompt Role | Model | Context |
|---------------|-------------------|-------|---------|
| Story analysis | "Project analyst" | Configurable | Story fields only |
| File selection (Pass 1) | "Senior {framework} developer" | Implementation model | Story + file tree |
| Implementation (Pass 2) | "Senior {framework} developer" | Implementation model | Story + selected file contents + history feedback |
| Test fix | "Debugging specialist" | Implementation model | Failed test output + relevant code |
| Code review | "Senior code reviewer" | Review model | Git diff + coding standards |

Each AI call already gets a focused persona, a scoped context window, and can use a different model. This is functionally equivalent to having separate agents — without the coordination overhead.

### 4. Debugging is traceable

When the pipeline fails today:
- One log file shows the full sequence
- The story context, AI response, test output, and fix attempts are all in the same timeline
- `dai history -s 12345` shows exactly what happened

With agents, a failure in the Review Agent might be caused by the Developer Agent producing a subtly wrong diff that the QA Agent didn't catch. Tracing across three independent agents with separate logs is significantly harder.

### 5. The fix loop is tight and fast

Current fix loop:
```
Test fails → parse errors → feed to AI → get fix → apply → retest
```

This is 1 function call within the pipeline. With agents:
```
QA Agent detects failure → serializes findings → sends to Developer Agent →
Developer Agent reconstructs context → generates fix → sends to QA Agent →
QA Agent re-runs tests → serializes results → ...
```

Each round-trip is a full agent invocation: prompt construction, API call, response parsing, artifact serialization. For 3 fix attempts, that's 6 agent invocations instead of 3 function calls.

### 6. Our current scale doesn't warrant it

The pipeline processes one story at a time sequentially. Even in batch mode (`dai run-all`), stories are processed one after another. The bottleneck is AI API latency and test execution time — neither of which agents solve. Adding agents would increase total token usage (due to context reconstruction at each handoff) without reducing wall-clock time.

---

## When to Implement Agents

### Trigger 1: Token limits on complex stories

**Signal:** Implementation prompts are being truncated or the AI is losing context on stories touching 10+ files.

**What to do:** Split the Developer capability into a Planning Agent and a Per-File Implementation Agent:

```
Planning Agent                    Per-File Implementation Agent (×N)
──────────────                    ────────────────────────────────────
Receives: story + full file tree  Receives: story summary + plan for THIS file + full file content
Returns: list of files + plan     Returns: code changes for THIS file
Context: story + tree (~4K tok)   Context: plan + one file (~8K tokens each)
```

This avoids cramming 10 file contents into one prompt. The pipeline still orchestrates, but each per-file call gets a clean context window.

**Estimated effort:** ~200 lines. Modify `implement.py` to loop over planned files with individual AI calls instead of one combined call.

### Trigger 2: Overnight batch throughput bottleneck

**Signal:** `dai run-all` with 30+ stories takes 6+ hours, and the team needs results by morning.

**What to do:** Parallelize independent stages across stories, not within a story:

```
Story A: Implement → Test ────────────────→ Review
Story B:              Implement → Test ───→ Review
Story C:                          Implement → Test → Review
```

This is pipeline parallelism (like a CPU pipeline), not agent independence. Each story still runs sequentially, but multiple stories overlap.

**Implementation:** Python `concurrent.futures` or `asyncio` with a worker pool. Not true agents — just concurrent pipeline instances with isolated git worktrees.

**Estimated effort:** ~300 lines. New `batch_runner.py` with worker pool, per-story git worktree isolation, shared SQLite access with WAL mode (already configured).

### Trigger 3: Cross-system localization workflows

**Signal:** Stories that span your TMS (Translation Management System), database, and content delivery need knowledge that doesn't fit in a single prompt's context alongside implementation code.

**What to do:** Create domain-specialized agents:

```
Translation Job Agent                    Content Sync Agent
─────────────────────                    ──────────────────
System prompt: TMS API expert            System prompt: DB schema expert
Context: TMS API docs, job formats,      Context: DB schema, migration patterns,
         language pair configurations              conflict resolution rules
Knows: how to create/monitor             Knows: how to write migrations,
       translation jobs                          handle encoding, resolve conflicts
```

These agents genuinely benefit from independence because they reason about different external systems. A single prompt cannot hold TMS API documentation + database schema + business rules + the source code being changed.

**Estimated effort:** ~500 lines. New `src/agents/` directory with base agent class, translation agent, sync agent, and an orchestrator that routes localization stories to the right agent.

### Trigger 4: Multi-developer concurrent use

**Signal:** 5+ developers running `dai` simultaneously against the same repository, causing branch conflicts and CI bottleneck.

**What to do:** Add per-run state isolation and a coordination layer:

```
Coordinator
    ├── Run 1 (Developer A, Story 100) — worktree A, branch feature/100
    ├── Run 2 (Developer B, Story 101) — worktree B, branch feature/101
    └── Run 3 (Developer C, Story 102) — worktree C, branch feature/102
```

This is closer to a job scheduler than an agent system. Each run is independent. The coordinator prevents branch collisions and manages worktree lifecycle.

**Estimated effort:** ~250 lines. This is Phase 6 item #22 (per-run state isolation) extended to multi-user.

---

## Architecture Comparison

| Aspect | Current (Single Pipeline) | Future (Multi-Agent) |
|--------|--------------------------|---------------------|
| **State management** | In-memory, single process | Serialized artifacts between agents |
| **Debugging** | One log, linear trace | Multiple logs, cross-agent tracing |
| **Token efficiency** | Context shared across calls | Context reconstructed at each handoff |
| **Fix loop latency** | 1 function call per retry | 2 agent invocations per retry |
| **Parallelism** | None (sequential) | Per-story pipeline parallelism |
| **Model flexibility** | ✅ Already supports per-stage models | ✅ Per-agent model selection |
| **Role specialization** | ✅ Per-call system prompts | ✅ Per-agent system prompts |
| **Failure isolation** | Pipeline stops, clear error | Agent fails, may cascade |
| **Code complexity** | ~1,500 lines total | ~3,000+ lines with agent framework |

---

## Decision

**Now:** Keep the single pipeline with role-based prompts. The sequential nature of the workflow, the tight fix loop, and the current scale (< 10 files per story, < 30 stories per batch) all favor simplicity.

**Revisit when:**
1. Implementation prompts hit token limits on 10+ file stories → split into per-file calls
2. Batch runs exceed 6 hours with 30+ stories → add pipeline parallelism
3. Localization workflows need TMS + DB knowledge in one story → add domain agents
4. Multiple developers use the system concurrently → add run isolation

Each trigger is independent. Implement only the one that's triggered, not all at once.
