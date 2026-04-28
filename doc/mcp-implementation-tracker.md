# MCP Implementation Tracker

> **Goal:** Replace custom tool-use loops with MCP servers, giving Claude live access to filesystem, Azure DevOps, and Git during all pipeline stages.
>
> **Prerequisite:** Claude Team plan + Claude CLI installed
>
> **Started:** 2026-04-27

---

## Phase 1: Filesystem MCP Server (Foundation)

**Goal:** Create MCP server with 4 core tools. Wire it into `claude -p --mcp-config`. Delete dual tool-use loops from `implement.py`.

**Estimated effort:** 3-4 days

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1.1 | Create `src/mcp/filesystem_server.py` with `read_file` tool | ✅ Done | Sandboxed to module_path. Line-range support. Char budget tracking. |
| 1.2 | Add `list_directory` tool | ✅ Done | Return file names + sizes. Sandboxed to module_path. |
| 1.3 | Add `write_file` tool | ✅ Done | Full-file replace. Backup before write. Restricted to feature branch. |
| 1.4 | Add `run_command` tool | ✅ Done | Whitelist only: test, lint, cache-clear. Auto-detects project type. |
| 1.5 | Create `mcp.json` config template | ✅ Done | Auto-generated per run via `src/mcp/config.py`. `.mcp.json.example` committed. |
| 1.6 | Add `mcp-sdk` dependency to `pyproject.toml` | ✅ Done | `mcp>=1.0` — installed v1.27.0 |
| 1.7 | Update `_try_claude_cli()` in `implement.py` to pass `--mcp-config` | ✅ Done | Both `_try_claude_code` and `_try_claude_code_plan` updated. |
| 1.8 | Delete `_tooluse_loop_anthropic()` from `implement.py` | ⬜ Not Started | ~100 lines removed. |
| 1.9 | Delete `_tooluse_loop_openai()` from `implement.py` | ⬜ Not Started | ~120 lines removed. |
| 1.10 | Delete `TOOLUSE_TOOLS` + `TOOLUSE_TOOLS_ANTHROPIC` definitions | ⬜ Not Started | ~80 lines removed. MCP server defines tools now. |
| 1.11 | Delete `_handle_tool_call()` dispatcher | ⬜ Not Started | ~50 lines removed. MCP routes automatically. |
| 1.12 | Write tests for filesystem MCP server | ✅ Done | 29 tests in `tests/test_mcp_servers.py` — all passing. |
| 1.13 | Integration test: `dai run` with MCP on a real story | ⬜ Not Started | Verify end-to-end: Claude CLI + MCP reads files, writes code. |
| 1.14 | Verify API fallback still works (single-shot, no tool-use) | ⬜ Not Started | API path stays as simple prompt→response, no MCP. |

**Lines removed:** ~350-400 from `implement.py`
**Lines added:** ~300 in `src/mcp/filesystem_server.py` + ~50 in config generation
**Net:** ~0 (parity), but unified and cleaner

**Definition of Done:**
- [ ] `dai run -s <story>` uses Claude CLI + MCP for implementation
- [ ] Claude can read/write files and run tests via MCP tools
- [ ] All existing tests pass
- [ ] New MCP server tests pass
- [ ] API fallback (no MCP) still works when CLI unavailable

---

## Phase 2: Azure DevOps MCP Server

**Goal:** Let Claude query ADO for related stories, read comments, check live state during analysis and implementation.

**Estimated effort:** 2 days

| # | Task | Status | Notes |
|---|------|--------|-------|
| 2.1 | Create `src/mcp/azure_devops_server.py` with `get_work_item` tool | ✅ Done | Wraps az CLI. Returns markdown-formatted details. |
| 2.2 | Add `query_work_items` tool | ✅ Done | Accepts WIQL query string. Returns list of matching items. Cap at 20 results. |
| 2.3 | Add `add_comment` tool | ✅ Done | Post HTML comment to work item. Rate limit: max 5 per session. |
| 2.4 | Add ADO server to `mcp.json` config | ✅ Done | Auto-populated from pipeline config. |
| 2.5 | Update analyzer `_try_claude_cli()` to use `--mcp-config` | ✅ Done | Analyzer + reviewer both pass `--mcp-config`. |
| 2.6 | Write tests for ADO MCP server | ✅ Done | 5 tests with mocked az CLI. Rate limit tested. |
| 2.7 | Test: spike story with related work items | ⬜ Not Started | Verify Claude queries linked stories for richer analysis. |

**Definition of Done:**
- [ ] Claude can query ADO work items during analysis
- [ ] Claude can look up related stories referenced in descriptions
- [ ] Rate limited to prevent ADO API abuse
- [ ] Existing ADO integration (`azure_devops.py`) unchanged — MCP wraps it

---

## Phase 3: Git MCP Server

**Goal:** Let Claude see its own diffs, check git status, and self-correct after test failures.

**Estimated effort:** 2 days

| # | Task | Status | Notes |
|---|------|--------|-------|
| 3.1 | Create `src/mcp/git_server.py` with `git_status` tool | ✅ Done | Returns `git status --short`. Read-only. |
| 3.2 | Add `git_diff` tool | ✅ Done | `git diff <base_branch>`. Truncate at 30k chars. |
| 3.3 | Add `git_log` tool | ✅ Done | `git log --oneline -n <count>`. Read-only. Cap 50. |
| 3.4 | Add `get_changed_files` tool | ✅ Done | Returns list of files changed vs base branch. |
| 3.5 | (Optional) Add `git_commit` tool | ⬜ Not Started | Restricted to current feature branch ONLY. No force-push. |
| 3.6 | Add git server to `mcp.json` config | ✅ Done | Auto-populated from pipeline config. |
| 3.7 | Update reviewer `_try_claude_cli()` to use `--mcp-config` | ✅ Done | Reviewer passes `--mcp-config` to Claude CLI. |
| 3.8 | Improve fix loop: Claude self-corrects via diff | ⬜ Not Started | Instead of feeding error text, Claude runs tests + reads diff itself. |
| 3.9 | Write tests for git MCP server | ✅ Done | 6 tests with real temp git repo. All passing. |
| 3.10 | Test: story with test failures + auto-fix | ⬜ Not Started | Verify Claude reads diff, identifies bug, fixes it. |

**Security constraints:**
- `git_commit` restricted to branches matching `feature-*` pattern
- No `git push`, `git reset --hard`, `git checkout <other-branch>`
- No force operations

**Definition of Done:**
- [ ] Claude can see its own changes via `git_diff`
- [ ] Fix loop uses MCP (Claude runs tests + reads diff) instead of text paste
- [ ] Branch isolation enforced — Claude cannot affect non-feature branches
- [ ] Reviewer gives better feedback (reads surrounding code, not just diff)

---

## Phase 4: Cleanup & Polish

**Goal:** Remove dead code, optimize MCP server performance, update docs.

**Estimated effort:** 1-2 days

| # | Task | Status | Notes |
|---|------|--------|-------|
| 4.1 | Remove dead API tool-use code from `implement.py` | ⬜ Not Started | Strategy 4 (multi-turn tool-use) no longer needed. |
| 4.2 | Simplify `implement.py` strategy chain | ⬜ Not Started | From 4 strategies → 2: Claude CLI+MCP → single-shot API. |
| 4.3 | Add MCP server health check to `dai doctor` | ⬜ Not Started | Verify MCP server starts, tools respond. |
| 4.4 | Add `--no-mcp` flag for fallback | ⬜ Not Started | Disable MCP for debugging (reverts to current behavior). |
| 4.5 | Add MCP tool call tracking to `history.py` | ⬜ Not Started | Log which tools Claude used per story, how many calls. |
| 4.6 | Update `config.yaml` with MCP settings | ⬜ Not Started | `mcp.enabled`, `mcp.tool_call_budget`, `mcp.servers`. |
| 4.7 | Update `copilot-instructions.md` with MCP architecture | ⬜ Not Started | So future dev sessions understand the MCP layer. |
| 4.8 | Run full test suite + real story end-to-end | ⬜ Not Started | Final validation. |

**Definition of Done:**
- [ ] `implement.py` under 800 lines (from current 1500)
- [ ] `dai doctor` validates MCP setup
- [ ] `dai history` shows tool call counts per story
- [ ] All 113+ tests pass
- [ ] One real story processed successfully end-to-end with MCP

---

## Summary

| Phase | Focus | Effort | Code Impact | Key Outcome |
|-------|-------|--------|-------------|-------------|
| **1** | Filesystem MCP | 3-4 days | -400, +350 lines | Claude reads/writes code via MCP tools |
| **2** | Azure DevOps MCP | 2 days | +200 lines | Claude queries related stories live |
| **3** | Git MCP | 2 days | +250 lines | Claude sees own diffs, self-corrects |
| **4** | Cleanup | 1-2 days | -400 lines | implement.py halved, strategy simplified |
| **Total** | | **~10 days** | **net -400 lines** | Unified MCP architecture |

---

## Architecture After MCP

```
┌─────────────────────────────────────────────────┐
│                  dai CLI                         │
│         (pipeline.py orchestrator)               │
└──────────────────┬──────────────────────────────┘
                   │
          claude -p --mcp-config mcp.json
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│Filesystem│ │  Azure   │ │   Git    │
│  Server  │ │  DevOps  │ │  Server  │
│          │ │  Server  │ │          │
│read_file │ │get_item  │ │git_diff  │
│list_dir  │ │query     │ │git_status│
│write_file│ │comment   │ │git_log   │
│run_cmd   │ │          │ │changed   │
└──────────┘ └──────────┘ └──────────┘
      │            │            │
      ▼            ▼            ▼
  Local FS     az CLI       git CLI
```

**Fallback (when CLI unavailable):**
```
pipeline.py → Anthropic/OpenAI API (single-shot, no tool-use)
```

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-27 | MCP over custom tool-use | Unified protocol, less code, future-proof |
| 2026-04-27 | Keep API fallback as single-shot only | MCP handles tool-use; API is just text→text backup |
| 2026-04-27 | 3 separate MCP servers (not 1 monolith) | Separation of concerns; can enable/disable per stage |
| 2026-04-27 | Phase 1 first (filesystem) | Highest code reduction, proves the pattern |
| | | |
