# Multi-Turn Tool Use — Technical Design

> **Status:** Implemented
> **Date:** April 1, 2026
> **Tests:** 18 new (113 total passing)

---

## Problem

The pipeline sends entire file contents to the AI in a single prompt. For story #1636226, this was ~40,592 characters (~10K+ tokens) including an 816-line `.module` file — when the AI only needed 20 lines from one function.

This causes two problems:
1. **GitHub Models API (copilot provider) rejects requests over 8K tokens** — the story couldn't be processed at all
2. **Token cost scales with file size, not story complexity** — simple fixes cost the same as complex refactors because full files are always sent

## Solution

Replace the upfront "read all files then send" approach with **multi-turn function calling** — the AI requests files on demand via tools, reading only what it needs.

### Before (Two-Pass — Expensive)

```
API Call 1: "Which files do you need?"              → AI returns file list
  Pipeline reads ALL those files from disk (30K chars)
API Call 2: Story + ALL file contents + rules       → AI returns plan
  Total input: ~40K chars (~10K tokens)
```

### After (Tool Use — Efficient)

```
API Call 1: Story + file tree + tools available
  → AI calls read_file("TmgmtCapiItemsCount.php")

API Call 2: Here's the 31-line file
  → AI calls read_file("tmgmt_contentapi.module", start_line=450, end_line=480)

API Call 3: Here's those 30 lines
  → AI returns implementation plan

  Total input: ~5K chars (~1.2K tokens) spread across 3 small calls
```

## Architecture

```
_implement_plan()
    │
    ├── 1. Try Claude Code CLI        (has filesystem — best)
    ├── 2. Try Codex CLI              (has filesystem — good)
    ├── 3. Try API with tool use      ← NEW (AI reads on demand — efficient)
    └── 4. Fall back to two-pass      (current behavior — expensive but reliable)
```

### Tool-Use Loop

```
┌──────────────────────────────────────────────────────┐
│                  _api_plan_tooluse()                   │
│                                                      │
│  Initial message:                                    │
│    System prompt + story context + file tree          │
│    Tools: [read_file, list_directory]                 │
│                                                      │
│  Loop (max 10 turns):                                │
│    ├─ AI responds with tool_calls?                   │
│    │   ├─ read_file(path, start?, end?)              │
│    │   │   → Pipeline reads from disk (automatic)    │
│    │   │   → Sends content back as tool result       │
│    │   │   → Continue loop                           │
│    │   └─ list_directory(path)                       │
│    │       → Pipeline lists dir contents             │
│    │       → Continue loop                           │
│    │                                                 │
│    └─ AI responds with text (final answer)?          │
│        → Parse as plan JSON                          │
│        → Return plan for approval                    │
│                                                      │
│  Budget: 15,000 chars total across all reads         │
│  Turns: max 10 round-trips                           │
└──────────────────────────────────────────────────────┘
```

## Tools Available to the AI

### read_file

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | File path relative to module root |
| `start_line` | integer | No | Start line (1-based) |
| `end_line` | integer | No | End line (1-based, inclusive) |

- Reads from disk — sandboxed to module directory (path traversal blocked)
- Line range support — AI can read specific functions instead of entire files
- Content deducted from 15K char budget
- When budget exhausted: returns "Budget exhausted — produce your plan"

### list_directory

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Directory path relative to module root (`.` for root) |

- Lists files and subdirectories
- Excludes: `vendor/`, `node_modules/`, `__pycache__/`, hidden files
- No budget cost — directory listings are free

## Provider Support

| Provider | Tool Call Format | Handled By |
|----------|-----------------|------------|
| **OpenAI** | `tools` param → `tool_calls` in response → `role: "tool"` replies | `_tooluse_loop_openai()` |
| **Copilot (GitHub Models)** | Same as OpenAI (OpenAI-compatible API) | `_tooluse_loop_openai()` |
| **Anthropic** | `tools` param → `tool_use` content blocks → `tool_result` replies | `_tooluse_loop_anthropic()` |

Both formats are handled natively using the respective SDKs. The tool definitions are identical in concept but use different wire formats (`TOOLUSE_TOOLS` for OpenAI, `TOOLUSE_TOOLS_ANTHROPIC` for Anthropic).

## Safety & Security

| Concern | Protection |
|---------|-----------|
| Path traversal | `full_path.resolve().relative_to(module_dir.resolve())` — rejects any path outside module |
| Infinite loops | Max 10 turns, then force final answer |
| Token explosion | 15K char budget across all tool reads |
| File writes | Tools can only read — no write tools exposed |
| User approval | Not required for tool reads (same as current `_read_file_contents`) |
| Fallback | If tool use fails, falls back to two-pass (existing behavior) |

## Trust Level Behavior

Tool calls are automatic in all trust levels — they're equivalent to the pipeline reading files from disk.

| Trust Level | Consent Prompt | Tool Calls | Plan Approval | Push |
|-------------|---------------|------------|---------------|------|
| `cautious` | Yes | Automatic | Yes | Yes |
| `balanced` | No | Automatic | Yes | Yes |
| `autonomous` | No | Automatic | No | Yes |
| `full-auto` | No | Automatic | No | No |

## Token Cost Comparison

Using story #1636226 as a reference (3 files, 889 total lines):

| Approach | API Calls | Input Tokens | Estimated Cost (GPT-4o) |
|----------|-----------|-------------|------------------------|
| Two-pass (before) | 2 | ~12,000 | ~$0.06 |
| Tool use (after) | 3-4 | ~3,000 | ~$0.015 |
| **Savings** | +1-2 calls | **75% less** | **75% cheaper** |

For a batch of 15 stories overnight:
- Before: ~180K input tokens → ~$0.90
- After: ~45K input tokens → ~$0.22
- **Savings: ~$0.68 per batch run**

## Fallback Chain

If tool use fails for any reason (provider doesn't support tools, API error, max turns without producing a plan), the system falls back to the existing two-pass approach automatically:

```python
# In _implement_plan():
result = self._api_plan_tooluse(story_context)  # Try tool use first
if result:
    return result
return self._api_plan(story_context)  # Fall back to two-pass
```

No existing behavior is broken. Tool use is additive.

## Files Changed

| File | Change |
|------|--------|
| `src/agent/implement.py` | Added tool definitions, `_api_plan_tooluse()`, `_run_tooluse_loop()`, `_tooluse_loop_openai()`, `_tooluse_loop_anthropic()`, `_handle_tool_call()`, `_tool_read_file()`, `_tool_list_directory()`. Updated `_implement_plan()` to try tool use before two-pass. |
| `tests/test_tool_use.py` | 18 tests covering: file reading, line ranges, path traversal, budget enforcement, directory listing, tool routing, budget tracking |

## Configuration

No new config needed. Tool use is used automatically when the API path is taken. The budget constants are in `implement.py`:

```python
MAX_TOOLUSE_CHARS = 15_000   # Total chars AI can read via tools
MAX_TOOLUSE_TURNS = 10       # Max round-trips before forcing plan output
```

These can be made configurable later if needed.
