"""Implementation agent — uses AI to implement story requirements."""

from __future__ import annotations

import json as _json
import logging
import re as _re
import subprocess
from pathlib import Path

import anthropic
import openai

logger = logging.getLogger("devops_ai_agent.implement")

# Maximum characters of file content to include in AI context.
# Claude Sonnet has a 200k token context window (~800k chars) — no need to cap aggressively.
MAX_CONTEXT_CHARS = 150_000

# Tool-use budget: max chars the AI can read via tool calls per session.
MAX_TOOLUSE_CHARS = 100_000
# Max round-trips for tool use before forcing the AI to produce output.
# Keep low on free tier (10k TPM): conversation history compounds every turn.
# Override via ai_agent.max_tooluse_turns in config.
#   Free tier : 6  (default) — ~3 reads + 1 write + buffer
#   Tier 1+   : 15 — comfortable for cross-file changes
MAX_TOOLUSE_TURNS = 6
# Max chars of story context to include in the tool-use initial prompt.
# Default is conservative (6k chars ≈ 1,500 tokens) to stay within the free
# tier rate limit (10k TPM). Raise via config ai_agent.max_prompt_chars once
# you upgrade to a paid tier (Tier 1 = 50k TPM, Tier 2 = 100k TPM).
MAX_TOOLUSE_PROMPT_CHARS = 6_000

def _parse_retry_after(exc: Exception, default: int = 60) -> int:
    """Extract the retry-after seconds from a rate limit exception response header."""
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            headers = getattr(response, "headers", {})
            for header in ("retry-after", "x-ratelimit-reset-requests"):
                val = headers.get(header)
                if val:
                    return max(1, int(float(val)))
    except Exception:
        pass
    return default


def _parse_retry_after_from_text(text: str, default: int = 65) -> int:
    """Extract a retry-after duration from CLI error text containing a 429 message."""
    # Look for patterns like "try again in 45s" or "retry after 60 seconds".
    m = _re.search(r'(?:try again|retry after|wait)\s+(?:in\s+)?(\d+)\s*s', text, _re.IGNORECASE)
    if m:
        return max(1, int(m.group(1)))
    return default


# Tool definitions for function calling (OpenAI-compatible format).
TOOLUSE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the project module. Returns file content. "
                "Optionally read a specific line range to save tokens."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the module root.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line (1-based). Omit to read from beginning.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line (1-based, inclusive). Omit to read to end.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in a directory within the module.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to module root. Use '.' for root.",
                    },
                },
                "required": ["path"],
            },
        },
    },
]

# Anthropic tool format (different from OpenAI).
TOOLUSE_TOOLS_ANTHROPIC = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the project module. Returns file content. "
            "Optionally read a specific line range to save tokens."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the module root."},
                "start_line": {"type": "integer", "description": "Start line (1-based). Omit to read from beginning."},
                "end_line": {"type": "integer", "description": "End line (1-based, inclusive). Omit to read to end."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in a directory within the module.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to module root. Use '.' for root."},
            },
            "required": ["path"],
        },
    },
]

# Agentic tools for Anthropic — superset of read-only tools, adds write + run.
# Used in auto approval_mode so Claude writes files directly without a plan-review step.
AGENTIC_TOOLS_ANTHROPIC = TOOLUSE_TOOLS_ANTHROPIC + [
    {
        "name": "write_file",
        "description": (
            "Write or overwrite a file in the project module. "
            "ALWAYS read_file first so you include all existing code plus your changes. "
            "Provide the complete file content — never partial content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to module root.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Complete file content to write. Must include ALL existing code "
                        "plus your additions/changes. Never omit existing functions or imports."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a test or lint command in the project workspace and return the output. "
            "Use this to verify your changes work before finishing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["test", "lint", "cache-clear"],
                    "description": (
                        "'test' — run the test suite, "
                        "'lint' — run the linter/code style checker, "
                        "'cache-clear' — clear the application cache."
                    ),
                },
            },
            "required": ["command"],
        },
    },
]


class ImplementationAgent:
    """AI agent that reads story context and implements code changes."""

    def __init__(self, config: dict):
        self.config = config
        ai = config.get("ai_agent", {})
        self.provider = ai.get("provider", "anthropic")
        self.model = ai.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = ai.get("max_tokens", 8192)
        self.temperature = ai.get("temperature", 0.2)
        self.require_consent = ai.get("require_consent", True)
        # Trust levels: balanced/autonomous/full-auto skip consent prompts.
        trust_level = ai.get("trust_level", "cautious")
        if trust_level in ("balanced", "autonomous", "full-auto"):
            self.require_consent = False
        self.approval_mode = ai.get("approval_mode", "plan-review")  # "auto", "plan-review"
        # CLI-only mode: only use Claude Code CLI, never fall through to API.
        # When CLI hits rate limits, wait patiently instead of burning API tokens.
        # Set to True when you have a Claude Team/Max subscription and want the
        # VS Code-like experience (AI reads/writes files directly).
        self.cli_only = ai.get("cli_only", False)
        # CLI retry settings (used when cli_only=True or CLI hits rate limits).
        self.max_cli_retries = ai.get("max_cli_retries", 3)
        self.cli_retry_base_wait = ai.get("cli_retry_base_wait", 60)  # seconds
        # Configurable prompt size limit — tune to your Anthropic API tier:
        #   Free / Build : 10k TPM  → keep at 6,000 (default)
        #   Tier 1 ($5+) : 50k TPM  → can raise to 30,000
        #   Tier 2 ($25+): 100k TPM → can raise to 80,000
        self.max_prompt_chars = ai.get("max_prompt_chars", MAX_TOOLUSE_PROMPT_CHARS)
        self.max_tooluse_turns = ai.get("max_tooluse_turns", MAX_TOOLUSE_TURNS)
        self.workspace_dir = Path(config["project"]["workspace_dir"])
        self.module_path = config["project"].get("module_path", "")
        self.story_id: int | None = None  # Set by pipeline before calling implement()
        self.ticket_logger = None  # Set by pipeline — TicketLogger instance for detailed I/O logs.
        self.analysis_hint: str = ""  # Set by pipeline — summary+approach+affected_areas from analysis stage.

    def _record_usage(self, response, stage: str) -> None:
        """Record token usage from an API response (OpenAI or Anthropic format)."""
        try:
            from src.history import save_token_usage
            usage = getattr(response, "usage", None)
            if not usage:
                return
            # OpenAI format: prompt_tokens, completion_tokens
            # Anthropic format: input_tokens, output_tokens
            prompt = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
            completion = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
            save_token_usage(
                story_id=self.story_id,
                stage=stage,
                provider=self.provider,
                model=self.model,
                prompt_tokens=prompt,
                completion_tokens=completion,
            )
            # Log to per-ticket log file.
            if self.ticket_logger:
                self.ticket_logger.token_usage(stage, self.provider, self.model, prompt, completion)
        except Exception as e:
            logger.debug("Failed to record token usage: %s", e)

    def implement(self, story_context: str) -> dict:
        """Run the AI agent to implement the story.

        Returns a dict with:
          - success: bool
          - method: str (which method was used)
          - output: str (agent output/summary)
        """
        # Fail fast if we're in a rate limit cooldown.
        from src.utils.rate_limit import check_cooldown
        cooldown_msg = check_cooldown(self.provider)
        if cooldown_msg:
            return {"success": False, "method": f"{self.provider}-cooldown", "output": cooldown_msg}
        if self.approval_mode == "auto":
            return self._implement_auto(story_context)
        # Default: plan-review mode — generates a plan only, does NOT write files.
        return self._implement_plan(story_context)

    @staticmethod
    def _is_simple_story(story_context: str) -> bool:
        """Heuristic: is this story simple enough to implement in a single API call?

        Simple stories have short context (< 2,000 chars) and no indicators of
        multi-file complexity. Simple stories skip the agentic/tool-use loop and
        go directly to the single-shot plan path — saving 80–90% of token cost.
        """
        if len(story_context) > 2_000:
            return False
        complexity_signals = [
            "multiple files", "across files", "refactor", "migrate",
            "restructure", "all instances", "every", "throughout",
        ]
        ctx_lower = story_context.lower()
        return not any(sig in ctx_lower for sig in complexity_signals)

    def _implement_auto(self, story_context: str) -> dict:
        """Auto mode — direct AI execution (writes files, no plan-review step).

        Strategy order:
        1. Claude Code CLI — best quality, lean prompt, retries on rate limit.
        2. Single-shot API plan — for stories where CLI is unavailable.
        3. Agentic API loop — only if plan says changes are needed but couldn't apply.

        When cli_only=True, only Strategy 1 is used. If CLI is unavailable or
        all retries exhausted, the pipeline fails instead of burning API tokens.
        """
        # Strategy 1: Try Claude Code CLI (best — uses lean prompt, retries on 429).
        result = self._try_claude_code(story_context)
        if result:
            return result

        # In cli_only mode, do NOT fall through to API — fail explicitly.
        if self.cli_only:
            msg = (
                "CLI-only mode: Claude Code CLI failed or unavailable. "
                "Not falling through to API. Check 'claude' is in PATH and "
                "your subscription has available quota. You can retry with 'dai run'."
            )
            logger.warning(msg)
            if self.ticket_logger:
                self.ticket_logger.section("CLI-Only Mode — Stopped")
                self.ticket_logger.write(msg)
            return {"success": False, "method": "cli-only(stopped)", "output": msg}

        # Strategy 2: Single-shot API plan (1 API call — cheap fallback).
        # Used when CLI is not available or all retries exhausted.
        if self.provider == "anthropic":
            logger.info("Falling back to single-shot API plan.")
            plan_result = self._api_plan(story_context)
            if plan_result.get("success"):
                plan = plan_result.get("plan")
                if plan and plan.file_changes:
                    from .plan import apply_plan
                    logger.info("Single-shot plan: %d file change(s): %s",
                                len(plan.file_changes),
                                [fc.path for fc in plan.file_changes])
                    if self.ticket_logger:
                        self.ticket_logger.section("Single-Shot Plan")
                        self.ticket_logger.kv("File changes", str(len(plan.file_changes)))
                        for fc in plan.file_changes:
                            self.ticket_logger.kv(f"  {fc.action}", f"{fc.path} ({fc.merge_strategy}, {len(fc.content)} chars)")
                    for fc in plan.file_changes:
                        fc.approved = True
                    plan.approved = True
                    apply_result = apply_plan(plan, self.workspace_dir, self.module_path)
                    logger.info("Single-shot apply: %d applied, %d skipped.",
                                apply_result.get("total_applied", 0), apply_result.get("total_skipped", 0))
                    if self.ticket_logger:
                        self.ticket_logger.kv("Applied", str(apply_result.get("applied", [])))
                        self.ticket_logger.kv("Skipped", str(apply_result.get("skipped", [])))
                    return {
                        "success": True,
                        "method": "anthropic-single-shot",
                        "output": plan_result.get("output", ""),
                    }
                else:
                    # AI analyzed the code and determined nothing to change.
                    # Do NOT fall through to agentic loop — it will just burn more tokens
                    # exploring the same codebase and reaching the same conclusion.
                    logger.info("AI determined no code changes needed for this story.")
                    if self.ticket_logger:
                        self.ticket_logger.section("No Changes Needed")
                        self.ticket_logger.write(plan_result.get("output", "AI found nothing to change."))
                    return {
                        "success": True,
                        "method": "anthropic-single-shot(no-changes)",
                        "output": plan_result.get("output", "AI analyzed the codebase and determined no changes are needed."),
                    }

        # Strategy 3: Try Codex CLI.
        result = self._try_codex_cli(story_context)
        if result:
            return result

        # Strategy 4: Fall back to API-based implementation.
        return self._api_implementation(story_context)

    def _implement_plan(self, story_context: str) -> dict:
        """Plan-review mode. Strategies ordered cheapest → most capable.

        Strategy 1 — CLI (free, surgical): Claude Code / Codex CLI reads and
          edits files directly. Zero API tokens for file access.

        Strategy 2 — Single-shot API (1 call): Python selects relevant files
          via keyword matching (free), reads them, sends story + file contents
          in ONE API call. Typical cost: ~2k–5k tokens total.
          This is the default path for Anthropic when no CLI is available.

        Strategy 3 — Multi-turn tool-use (multiple calls): AI explores the
          codebase interactively. Higher quality for complex stories but costs
          3–5× more tokens due to conversation history accumulation.
          Only used as escalation when Strategy 2 returns empty results.
        """
        # Strategy 1: CLI tools — no API tokens for file access.
        result = self._try_claude_code_plan(story_context)
        if result:
            return result

        result = self._try_codex_cli_plan(story_context)
        if result:
            return result

        # In cli_only mode, do NOT fall through to API — fail explicitly.
        if self.cli_only:
            msg = (
                "CLI-only mode: Claude Code CLI failed or unavailable. "
                "Not falling through to API. Check 'claude' is in PATH and "
                "your subscription has available quota. You can retry with 'dai run'."
            )
            logger.warning(msg)
            return {"success": False, "method": "cli-only(stopped)", "output": msg}

        # Strategy 2: Single-shot API with Python-selected context (cheapest API path).
        # Python does the file selection and reading — only ONE API call is made.
        result = self._api_plan(story_context)
        if result.get("success"):
            return result

        # Strategy 3: Multi-turn tool-use — escalate only if single-shot failed.
        # This costs more tokens but handles complex cross-file stories better.
        logger.info("Single-shot plan failed — escalating to multi-turn tool-use.")
        result = self._api_plan_tooluse(story_context)
        if result:
            return result

        return result  # Return the failed single-shot result as the final answer.

    def _try_claude_code_plan(self, story_context: str) -> dict | None:
        """Plan-review via Claude Code CLI — CLI reads files itself.

        Uses the same lean prompt as _try_claude_code but asks for JSON plan output.
        """
        if not _command_exists("claude"):
            return None

        from .plan import PLAN_JSON_SCHEMA, parse_plan_response

        # Extract title.
        title = ""
        for line in story_context.splitlines():
            if "**Title:**" in line:
                title = line.split("**Title:**")[-1].strip()
                break

        lean = self._build_lean_prompt(title, story_context, self.module_path, self.analysis_hint)

        prompt = (
            f"{lean}\n\n"
            f"Return your implementation plan as ONLY valid JSON matching this schema:\n"
            f"```json\n{PLAN_JSON_SCHEMA}\n```\n\n"
            f"For each file you modify, READ it first and provide the COMPLETE updated file "
            f"with merge_strategy='replace'."
        )

        logger.info("Claude Code CLI plan prompt: %d chars.", len(prompt))

        if self.require_consent:
            from src.utils.data_consent import request_consent
            approved = request_consent(
                action="Generate plan via Claude Code CLI",
                provider="claude-code-cli",
                model="local CLI",
                data_summary=[
                    ("Prompt", f"Lean prompt ({len(prompt)} chars)"),
                    ("Module path", self.module_path),
                ],
                full_payload=prompt,
            )
            if not approved:
                return None

        try:
            cmd = ["claude", "-p"]
            # Attach MCP config if available.
            from src.mcp.config import get_mcp_config_path
            mcp_config = get_mcp_config_path()
            if mcp_config:
                cmd.extend(["--mcp-config", str(mcp_config)])
            result = subprocess.run(
                cmd, input=prompt, cwd=self.workspace_dir,
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0 and result.stdout.strip():
                plan = parse_plan_response(result.stdout)
                for fc in plan.file_changes:
                    if fc.action == "modify":
                        fc.merge_strategy = "replace"
                return {
                    "success": True,
                    "method": "claude-code-plan",
                    "output": plan.to_markdown(),
                    "plan": plan,
                }
            else:
                logger.warning("Claude Code CLI plan: exit code %d", result.returncode)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("Claude Code CLI plan generation failed or timed out.")
        return None

    def _try_codex_cli_plan(self, story_context: str) -> dict | None:
        """Plan-review via Codex CLI — CLI reads files itself."""
        if not _command_exists("codex"):
            return None

        from .plan import PLAN_JSON_SCHEMA, parse_plan_response

        logger.info("Using Codex CLI for plan generation (reads files directly)...")
        prompt = (
            f"Read the following story context and produce an implementation plan "
            f"for the module at '{self.module_path}'.\n\n"
            f"IMPORTANT: You have full filesystem access. READ each file you plan to "
            f"modify so you understand its current contents. Then produce a plan with "
            f"the COMPLETE updated file content for each change.\n\n"
            f"For each file_change, set merge_strategy='replace' (you have the full files).\n\n"
            f"Return ONLY valid JSON matching this schema:\n"
            f"```json\n{PLAN_JSON_SCHEMA}\n```\n\n"
            f"## Story Context\n\n{story_context}"
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Generate plan via Codex CLI (reads files from filesystem)",
                provider="codex-cli",
                model="local CLI",
                data_summary=[
                    ("Story context", "Work item title, description, acceptance criteria, comments"),
                    ("Module path", self.module_path),
                    ("✅ Reads files", "CLI reads existing code directly — no guessing"),
                    ("✅ Safety", "Plan only — no file writes until approved"),
                ],
                full_payload=prompt,
            )
            if not approved:
                return None

        try:
            result = subprocess.run(
                ["codex", "--approval-mode", "suggest", prompt],
                cwd=self.workspace_dir,
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0 and result.stdout.strip():
                plan = parse_plan_response(result.stdout)
                for fc in plan.file_changes:
                    if fc.action == "modify":
                        fc.merge_strategy = "replace"
                return {
                    "success": True,
                    "method": "codex-cli-plan",
                    "output": plan.to_markdown(),
                    "plan": plan,
                }
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("Codex CLI plan generation failed or timed out.")
        return None

    def _api_plan(self, story_context: str) -> dict:
        """Plan-review via API — two-pass context-aware implementation.

        Pass 1 (file selection): AI identifies which files are relevant.
        Pass 2 (implementation): AI sees actual file contents and produces a plan.

        Files <500 lines → merge_strategy='replace' (AI provides complete file).
        Files ≥500 lines → merge_strategy='append' (AI provides only additions).
        Falls back to append-only strategy if file selection fails.
        """
        from .plan import PLAN_JSON_SCHEMA, parse_plan_response

        logger.info("Using %s API for plan generation (two-pass context-aware)...", self.provider)

        system_prompt = self._load_system_prompt()
        module_summary = self._get_module_summary()

        # --- Pass 1: File selection ---
        selected_files = self._select_relevant_files(story_context, module_summary)

        # --- Read selected files from disk ---
        file_contents = ""
        line_counts: dict[str, int] = {}
        if selected_files:
            logger.info("Pass 1 selected %d file(s): %s", len(selected_files), ", ".join(selected_files))
            file_contents, line_counts = self._read_file_contents(selected_files)
            logger.info("Read %d file(s) (%d chars).", len(line_counts), len(file_contents))

        has_context = bool(file_contents)

        # --- Pass 2: Implementation plan ---
        if has_context:
            small_files = [f for f, n in line_counts.items() if n < 500]
            large_files = [f for f, n in line_counts.items() if n >= 500]

            merge_rules = (
                "**CRITICAL RULES for file changes (you HAVE the file contents above):**\n"
                "- For NEW files (action='create'): provide FULL file content, set merge_strategy='replace'.\n"
            )
            if small_files:
                merge_rules += (
                    f"- For files UNDER 500 lines ({', '.join(small_files)}): provide the COMPLETE "
                    f"updated file with your changes integrated into the existing code. "
                    f"Set merge_strategy='replace'. Include ALL existing code plus your changes.\n"
                )
            if large_files:
                merge_rules += (
                    f"- For files 500+ lines ({', '.join(large_files)}): provide ONLY the new code "
                    f"to ADD. Set merge_strategy='append'.\n"
                )
            merge_rules += (
                "- IMPORTANT: For 'replace' strategy, you MUST include ALL existing code plus "
                "your changes. Do NOT omit existing functions, classes, or imports.\n"
            )

            plan_prompt = (
                f"## Story Context\n\n{story_context}\n\n"
                f"## Current File Contents\n\n{file_contents}\n\n"
                f"## Module Structure\n\n{module_summary}\n\n"
                f"## Task\n\n"
                f"Produce a **structured implementation plan** as JSON.\n\n"
                f"{merge_rules}\n"
                f"Return ONLY valid JSON matching this schema:\n"
                f"```json\n{PLAN_JSON_SCHEMA}\n```\n"
            )
        else:
            # No context — fall back to append-only strategy.
            logger.info("No file context available — using append-only strategy.")
            plan_prompt = (
                f"## Story Context\n\n{story_context}\n\n"
                f"## Module Structure\n\n{module_summary}\n\n"
                f"## Task\n\n"
                f"Analyze this story and produce a **structured implementation plan** as JSON.\n"
                f"Do NOT implement the changes yet — only provide the plan.\n\n"
                f"**CRITICAL RULES for file changes (you do NOT have filesystem access):**\n"
                f"- For NEW files (action=create): provide the FULL file content, set merge_strategy='replace'.\n"
                f"- For EXISTING files (action=modify): provide ONLY the new code to ADD "
                f"(new functions, hooks, use statements, etc.), NOT the full file. "
                f"Set merge_strategy='append'. The pipeline will append your code to the existing file.\n"
                f"- NEVER include existing code from the file — you do not have the file contents "
                f"and guessing will destroy existing code.\n"
                f"- If a modification absolutely requires replacing the entire file, "
                f"set merge_strategy='replace' and provide the complete file content.\n\n"
                f"Return ONLY valid JSON matching this schema:\n"
                f"```json\n{PLAN_JSON_SCHEMA}\n```\n"
            )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            data_items = [
                ("Story context", "Work item title, description, acceptance criteria, comments"),
                ("Module file tree", f"{self.module_path} — file names and directory structure"),
            ]
            if has_context:
                data_items.append(("File contents", f"{len(line_counts)} file(s) read from disk ({len(file_contents):,} chars)"))
                data_items.append(("✅ Context-aware", "AI sees actual code — will produce accurate changes"))
            data_items.extend([
                ("System prompt", "Development instructions"),
                ("✅ Safety", "AI will return a plan only — no files will be modified"),
            ])

            approved = request_consent(
                action=f"Generate plan via API ({'context-aware' if has_context else 'no filesystem access'})",
                provider=self.provider,
                model=self.model,
                data_summary=data_items,
                full_payload=system_prompt + "\n\n" + plan_prompt,
            )
            if not approved:
                return {
                    "success": False,
                    "method": f"{self.provider}-plan",
                    "output": "User denied consent to send data.",
                }

        try:
            response = self._call_ai(system_prompt, plan_prompt)

            plan = parse_plan_response(response)
            method = f"{self.provider}-plan(ctx)" if has_context else f"{self.provider}-plan"
            return {
                "success": True,
                "method": method,
                "output": plan.to_markdown(),
                "plan": plan,
                "context_files": list(line_counts.keys()) if has_context else [],
            }
        except Exception as e:
            logger.error("AI plan generation failed: %s", e)
            return {
                "success": False,
                "method": f"{self.provider}-plan",
                "output": str(e),
            }

    def _api_agentic_tooluse(self, story_context: str) -> dict | None:
        """Agentic mode for Anthropic — Claude reads, writes, and tests directly.

        Claude uses AGENTIC_TOOLS_ANTHROPIC (read_file, list_directory, write_file,
        run_command) to implement the story end-to-end. Files are written to disk
        during the loop; no plan JSON is generated or reviewed. Should only be
        called in auto approval_mode.

        Returns None if not applicable (wrong provider) or on failure.
        """
        if self.provider != "anthropic":
            return None

        logger.info("Using Anthropic agentic tool-use (read + write + test loop)...")

        system_prompt = (
            "You are an expert developer implementing a feature or bug fix directly in the codebase.\n"
            "You have tools to explore, read, write, and test files.\n\n"
            "IMPORTANT CONSTRAINT: You have a LIMITED number of tool turns. Be efficient.\n"
            "- Do NOT explore the entire directory tree. Target only the files you need.\n"
            "- Read a file, then IMMEDIATELY write the updated version in the same turn if possible.\n"
            "- If you need to create new files, do it right away — don't spend turns reading first.\n\n"
            "Workflow:\n"
            "1. Use list_directory ONCE to see the module structure (1 turn).\n"
            "2. Read ONLY the files you plan to modify (1-2 turns).\n"
            "3. Use write_file to apply changes — ALWAYS include the complete file content (1-2 turns).\n"
            "4. Optionally run_command('lint') to verify (1 turn).\n"
            "5. Reply with a concise summary of what you changed and why.\n\n"
            "CRITICAL: For every file you modify, read it first to preserve existing code.\n"
            "Make minimal, surgical changes. Never rewrite a file from scratch unless it's new."
        )

        user_prompt = (
            f"## Story Context\n\n{story_context}\n\n"
            f"## Task\n\n"
            f"Implement this story using the available tools. "
            f"Module root: `{self.module_path}`\n\n"
            f"You have at most {self.max_tooluse_turns} tool turns — be efficient. "
            f"Start with `list_directory('.')`, then read only the files you need to change, "
            f"then write the updated files. Do not over-explore."
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Implement via Anthropic agentic tool-use (reads + writes files directly)",
                provider=self.provider,
                model=self.model,
                data_summary=[
                    ("Story context", f"Work item title, description, acceptance criteria ({len(story_context):,} chars)"),
                    ("Module path", self.module_path),
                    ("Write access", "Claude will read AND write files in the module directory"),
                    ("Self-testing", "Claude will run lint/test commands and self-correct"),
                ],
                full_payload=system_prompt + "\n\n" + user_prompt,
            )
            if not approved:
                return None

        try:
            summary, files_written = self._run_agentic_loop_anthropic(system_prompt, user_prompt)
            if summary is None:
                return None

            output = f"Agentic implementation complete.\n"
            if files_written:
                output += f"Files written: {', '.join(files_written)}\n\n"
            else:
                logger.warning("Agentic loop completed but wrote 0 files. "
                               "Claude may have run out of turns before reaching write_file. "
                               "Consider increasing max_tooluse_turns in config.")
                output += "WARNING: No files were written. Claude may need more turns.\n\n"
            output += summary
            return {
                "success": True,
                "method": "anthropic-agentic",
                "output": output,
            }
        except Exception as e:
            logger.error("Anthropic agentic tool-use failed: %s", e)
            return None

    def _run_agentic_loop_anthropic(self, system_prompt: str, user_prompt: str) -> tuple[str | None, list[str]]:
        """Run the agentic Anthropic loop with write_file + run_command tools.

        Returns (summary_text, files_written). summary_text is None on failure.

        Adaptive pacing: inter-turn delay starts at 3s and doubles after each
        rate limit hit, spreading token spend across the rate-limit window.
        """
        import time

        client = anthropic.Anthropic()
        messages = [{"role": "user", "content": user_prompt}]
        chars_read = 0
        files_written: list[str] = []
        # Adaptive inter-turn delay: increases after each rate limit hit.
        inter_turn_delay = 3  # seconds; doubles on rate limit

        max_turns = self.max_tooluse_turns

        for turn in range(max_turns):
            # Pace every turn to spread token usage across the rate-limit window.
            if turn > 0:
                logger.debug("Agentic loop: sleeping %ds between turns.", inter_turn_delay)
                time.sleep(inter_turn_delay)

            # Retry the API call on rate limit (wait the retry-after period then retry).
            response = None
            for api_attempt in range(4):
                try:
                    response = client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                        system=system_prompt,
                        messages=messages,
                        tools=AGENTIC_TOOLS_ANTHROPIC,
                    )
                    break  # Success — exit retry loop.
                except anthropic.RateLimitError as e:
                    if api_attempt == 3:
                        from ..utils.rate_limit import record_rate_limit
                        record_rate_limit("anthropic", e)
                        logger.error("Rate limit persists after %d retries. Giving up.", api_attempt + 1)
                        return None, files_written
                    wait = _parse_retry_after(e, default=60)
                    # Double the inter-turn delay so future turns are paced more slowly.
                    inter_turn_delay = min(inter_turn_delay * 2, 120)
                    logger.warning(
                        "Rate limit hit (turn %d, attempt %d/4). Waiting %ds then retrying. "
                        "Inter-turn delay raised to %ds.",
                        turn, api_attempt + 1, wait, inter_turn_delay,
                    )
                    time.sleep(wait)
                except anthropic.APIStatusError as e:
                    logger.error("Anthropic API error during agentic loop (turn %d): %s", turn, e)
                    if turn == 0:
                        return None, files_written
                    # On later turns, request a summary of what was done so far.
                    break
                except Exception as e:
                    logger.error("Unexpected error during agentic loop (turn %d): %s", turn, e)
                    return None, files_written

            if response is None:
                break

            self._record_usage(response, f"implement-agentic-{turn}")

            # Log Claude's full response (text + tool calls) for this turn.
            text_blocks = [b.text for b in response.content if b.type == "text"]
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if self.ticket_logger:
                self.ticket_logger.section(f"Agentic Turn {turn} — Claude Response")
                if text_blocks:
                    self.ticket_logger.write("Claude said:\n" + "\n".join(text_blocks))
                if tool_use_blocks:
                    tool_names = [f"{b.name}({b.input.get('path', b.input.get('command', ''))!r})"
                                  for b in tool_use_blocks if isinstance(b.input, dict)]
                    self.ticket_logger.write(f"Tool calls requested: {', '.join(tool_names)}")
                if not text_blocks and not tool_use_blocks:
                    self.ticket_logger.write("(empty response)")
                self.ticket_logger.kv("Stop reason", response.stop_reason or "N/A")

            if not tool_use_blocks:
                # No more tool calls — extract final summary text.
                final_text = "\n".join(text_blocks) or "Done."
                if self.ticket_logger:
                    self.ticket_logger.section("Agentic Loop — Final Summary")
                    self.ticket_logger.write(final_text)
                    self.ticket_logger.kv("Files written", str(files_written) if files_written else "NONE")
                return final_text, files_written

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in tool_use_blocks:
                fn_name = block.name
                args = block.input if isinstance(block.input, dict) else {}

                if fn_name == "write_file":
                    result, chars_used = self._handle_tool_call(fn_name, args, chars_read)
                    written_path = args.get("path", "")
                    if written_path and not result.startswith("Error"):
                        files_written.append(written_path)
                        logger.info("Agentic write_file: %s", written_path)
                    elif self.ticket_logger:
                        self.ticket_logger.error(f"write_file failed for '{written_path}': {result}")
                else:
                    result, chars_used = self._handle_tool_call(fn_name, args, chars_read)
                    chars_read += chars_used

                    logger.info("Agentic tool call: %s(%s) → %d chars",
                                fn_name, args.get("path", args.get("command", ".")), chars_used)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        # Max turns reached — ask for final summary.
        logger.warning("Agentic loop max turns (%d) reached. Requesting summary.", max_turns)
        if self.ticket_logger:
            self.ticket_logger.section("Agentic Loop — Max Turns Reached")
            self.ticket_logger.kv("Turns used", str(max_turns))
            self.ticket_logger.kv("Files written so far", str(files_written) if files_written else "NONE")
            self.ticket_logger.kv("Chars read", str(chars_read))

        messages.append({"role": "user", "content": "Please summarise the changes you have made so far."})
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt,
                messages=messages,
            )
            self._record_usage(response, "implement-agentic-final")
            text_blocks = [b.text for b in response.content if b.type == "text"]
            final_text = "\n".join(text_blocks) or "Max turns reached."
            if self.ticket_logger:
                self.ticket_logger.section("Agentic Loop — Final Summary from Claude")
                self.ticket_logger.write(final_text)
            return final_text, files_written
        except Exception as e:
            logger.error("Agentic final summary failed: %s", e)
            if self.ticket_logger:
                self.ticket_logger.error(f"Final summary request failed: {e}")
            return "Max turns reached — check changed files.", files_written

    def _api_plan_tooluse(self, story_context: str) -> dict | None:
        """Plan generation via multi-turn tool use — AI reads files on demand.

        Instead of sending all file contents upfront, the AI uses read_file
        and list_directory tools to fetch only what it needs. Each tool call
        is a pipeline-local disk read — no user approval required.

        Returns None if tool use is not supported or fails, so the caller
        can fall back to the two-pass approach.
        """
        from .plan import PLAN_JSON_SCHEMA, parse_plan_response

        logger.info("Attempting tool-use plan generation via %s...", self.provider)

        system_prompt = self._load_system_prompt()

        # Truncate story context to fit within the provider's token budget.
        # The AI can use list_directory and read_file to explore the codebase,
        # so we don't need the module file listing in the initial prompt.
        slim_context = story_context
        if len(slim_context) > self.max_prompt_chars:
            slim_context = slim_context[:self.max_prompt_chars] + "\n\n... (story truncated — use tools to explore the codebase)\n"
            logger.info("Story context truncated from %d to %d chars for tool-use prompt.",
                        len(story_context), self.max_prompt_chars)

        user_prompt = (
            f"## Story Context\n\n{slim_context}\n\n"
            f"## Task\n\n"
            f"Implement this story. Start by using `list_directory` (path='.') to see "
            f"the module structure, then use `read_file` to read files you need to "
            f"understand. Read only what you need — prefer specific line ranges for "
            f"large files.\n\n"
            f"After reading the relevant files, produce a **structured implementation plan** as JSON.\n\n"
            f"**RULES for file changes:**\n"
            f"- For NEW files (action='create'): provide FULL file content, merge_strategy='replace'.\n"
            f"- For EXISTING files you have read in full (<500 lines): provide the COMPLETE "
            f"updated file with ALL existing code plus your changes. merge_strategy='replace'.\n"
            f"- For EXISTING files that are large (500+ lines) or you read only a section: "
            f"provide ONLY the new code to ADD. merge_strategy='append'.\n"
            f"- IMPORTANT: For 'replace', include ALL existing code plus your changes.\n\n"
            f"Return ONLY valid JSON matching this schema:\n"
            f"```json\n{PLAN_JSON_SCHEMA}\n```\n"
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Generate plan via API with tool use (AI reads files on demand)",
                provider=self.provider,
                model=self.model,
                data_summary=[
                    ("Story context", f"Work item title, description, acceptance criteria ({len(slim_context):,} chars)"),
                    ("System prompt", "Development instructions"),
                    ("🔧 Tool use", "AI will request files via read_file/list_directory tools"),
                    ("✅ Safety", "Tools only read from disk — no writes. Plan returned for review."),
                ],
                full_payload=system_prompt + "\n\n" + user_prompt,
            )
            if not approved:
                return None

        try:
            response_text = self._run_tooluse_loop(system_prompt, user_prompt)
            if not response_text:
                return None

            plan = parse_plan_response(response_text)
            return {
                "success": True,
                "method": f"{self.provider}-plan(tooluse)",
                "output": plan.to_markdown(),
                "plan": plan,
            }
        except Exception as e:
            logger.warning("Tool-use plan generation failed: %s — falling back to two-pass.", e)
            return None

    def _run_tooluse_loop(self, system_prompt: str, user_prompt: str) -> str | None:
        """Execute the multi-turn tool-use conversation loop.

        Returns the AI's final text response (the plan JSON), or None on failure.
        """
        chars_read = 0

        if self.provider == "anthropic":
            return self._tooluse_loop_anthropic(system_prompt, user_prompt, chars_read)
        else:
            # OpenAI and Copilot use the same format.
            return self._tooluse_loop_openai(system_prompt, user_prompt, chars_read)

    def _tooluse_loop_openai(self, system_prompt: str, user_prompt: str, chars_read: int) -> str | None:
        """Multi-turn tool-use loop for OpenAI/Copilot providers."""
        import time

        if self.provider == "copilot":
            token = _get_github_token()
            client = openai.OpenAI(
                base_url="https://models.github.ai/inference/v1",
                api_key=token,
            )
        else:
            client = openai.OpenAI()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        for turn in range(MAX_TOOLUSE_TURNS):
            last_error = None
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                        messages=messages,
                        tools=TOOLUSE_TOOLS,
                    )
                    last_error = None
                    break
                except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as e:
                    last_error = e
                    wait = [5, 20, 60][attempt]  # Longer backoff for rate limits
                    logger.warning("Tool-use API attempt %d failed (%s), retrying in %ds...", attempt + 1, type(e).__name__, wait)
                    time.sleep(wait)

            if last_error:
                if isinstance(last_error, openai.RateLimitError):
                    from src.utils.rate_limit import record_rate_limit
                    record_rate_limit(self.provider, last_error)
                logger.error("Tool-use API failed after 3 attempts: %s", last_error)
                return None

            choice = response.choices[0]
            message = choice.message
            self._record_usage(response, f"implement-tooluse-{turn}")

            # If the model returns text with no tool calls, we're done.
            if choice.finish_reason == "stop" or not message.tool_calls:
                return message.content

            # Process tool calls.
            messages.append(message)  # Add assistant message with tool_calls.

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    args = _json.loads(tool_call.function.arguments)
                except _json.JSONDecodeError:
                    args = {}

                result, chars_used = self._handle_tool_call(fn_name, args, chars_read)
                chars_read += chars_used

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

                logger.info("Tool call: %s(%s) → %d chars (budget: %d/%d)",
                            fn_name, args.get("path", args.get(".")),
                            chars_used, chars_read, MAX_TOOLUSE_CHARS)

        # Max turns exhausted — ask for final answer without tools.
        logger.warning("Tool-use max turns (%d) reached. Requesting final plan.", MAX_TOOLUSE_TURNS)
        messages.append({"role": "user", "content": "Please produce your implementation plan now with the information you have."})
        try:
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=messages,
            )
            self._record_usage(response, "implement-tooluse-final")
            return response.choices[0].message.content
        except Exception as e:
            logger.error("Final tool-use response failed: %s", e)
            return None

    def _tooluse_loop_anthropic(self, system_prompt: str, user_prompt: str, chars_read: int) -> str | None:
        """Multi-turn tool-use loop for Anthropic provider."""
        client = anthropic.Anthropic()

        messages = [{"role": "user", "content": user_prompt}]

        for turn in range(MAX_TOOLUSE_TURNS):
            try:
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=system_prompt,
                    messages=messages,
                    tools=TOOLUSE_TOOLS_ANTHROPIC,
                )
            except anthropic.RateLimitError as e:
                from ..utils.rate_limit import record_rate_limit
                record_rate_limit("anthropic", e)
                return None
            except Exception as e:
                logger.error("Anthropic tool-use call failed: %s", e)
                return None

            self._record_usage(response, f"implement-tooluse-{turn}")

            # Check if the response contains tool use blocks.
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks:
                # No tool calls — extract text response.
                text_blocks = [b.text for b in response.content if b.type == "text"]
                return "\n".join(text_blocks) if text_blocks else None

            # Build assistant message and tool results.
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in tool_use_blocks:
                fn_name = block.name
                args = block.input if isinstance(block.input, dict) else {}

                result, chars_used = self._handle_tool_call(fn_name, args, chars_read)
                chars_read += chars_used

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

                logger.info("Tool call: %s(%s) → %d chars (budget: %d/%d)",
                            fn_name, args.get("path", "."),
                            chars_used, chars_read, MAX_TOOLUSE_CHARS)

            messages.append({"role": "user", "content": tool_results})

            # If response also had stop_reason end_turn with tool use, continue.
            if response.stop_reason == "end_turn" and not tool_use_blocks:
                break

        # Max turns — force final answer.
        logger.warning("Tool-use max turns (%d) reached. Requesting final plan.", MAX_TOOLUSE_TURNS)
        messages.append({"role": "user", "content": "Please produce your implementation plan now with the information you have."})
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt,
                messages=messages,
            )
            self._record_usage(response, "implement-tooluse-final")
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks) if text_blocks else None
        except Exception as e:
            logger.error("Final Anthropic tool-use response failed: %s", e)
            return None

    def _handle_tool_call(self, fn_name: str, args: dict, chars_used: int) -> tuple[str, int]:
        """Execute a tool call and return (result_text, chars_consumed).

        Tools are sandboxed to the module directory with path traversal protection.
        write_file and run_command are only reachable in agentic mode.
        """
        module_dir = self.workspace_dir / self.module_path

        if fn_name == "read_file":
            result, chars = self._tool_read_file(module_dir, args, chars_used)
        elif fn_name == "list_directory":
            result, chars = self._tool_list_directory(module_dir, args)
        elif fn_name == "write_file":
            result, chars = self._tool_write_file(module_dir, args)
        elif fn_name == "run_command":
            result, chars = self._tool_run_command(args)
        else:
            return f"Unknown tool: {fn_name}", 0

        # Log every tool call to the per-ticket log.
        if self.ticket_logger:
            self.ticket_logger.tool_call(fn_name, args, result, chars)
        return result, chars

    def _tool_read_file(self, module_dir: Path, args: dict, chars_used: int) -> tuple[str, int]:
        """Read a file (or section) from disk. Returns (content, chars_consumed)."""
        rel_path = args.get("path", "")
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        full_path = (module_dir / rel_path).resolve()

        # Security: ensure path stays within module directory.
        try:
            full_path.relative_to(module_dir.resolve())
        except ValueError:
            return f"Error: path '{rel_path}' is outside the module directory.", 0

        if not full_path.exists():
            return f"Error: file '{rel_path}' not found.", 0
        if not full_path.is_file():
            return f"Error: '{rel_path}' is not a file. Use list_directory instead.", 0

        # Budget check.
        remaining = MAX_TOOLUSE_CHARS - chars_used
        if remaining <= 0:
            return "Budget exhausted — produce your plan with the information you have.", 0

        try:
            content = full_path.read_text()
        except Exception as e:
            return f"Error reading '{rel_path}': {e}", 0

        # Apply line range if specified.
        if start_line is not None or end_line is not None:
            lines = content.splitlines(keepends=True)
            s = max((start_line or 1) - 1, 0)
            e = end_line or len(lines)
            content = "".join(lines[s:e])

        # Truncate if exceeds remaining budget.
        if len(content) > remaining:
            content = content[:remaining] + "\n... (truncated — budget limit reached)"

        return content, len(content)

    def _tool_list_directory(self, module_dir: Path, args: dict) -> tuple[str, int]:
        """List contents of a directory. Returns (listing, 0) — no budget cost."""
        rel_path = args.get("path", ".")
        target = (module_dir / rel_path).resolve()

        try:
            target.relative_to(module_dir.resolve())
        except ValueError:
            return f"Error: path '{rel_path}' is outside the module directory.", 0

        if not target.exists():
            return f"Error: directory '{rel_path}' not found.", 0
        if not target.is_dir():
            return f"Error: '{rel_path}' is not a directory.", 0

        entries = []
        for p in sorted(target.iterdir()):
            if p.name.startswith("."):
                continue
            if p.name in ("vendor", "node_modules", "__pycache__"):
                continue
            name = p.name + "/" if p.is_dir() else p.name
            entries.append(name)

        return "\n".join(entries) if entries else "(empty directory)", 0

    def _tool_write_file(self, module_dir: Path, args: dict) -> tuple[str, int]:
        """Write a file to disk. Sandboxed to module directory."""
        rel_path = args.get("path", "")
        content = args.get("content", "")

        if not rel_path:
            return "Error: 'path' is required.", 0
        if not content:
            return "Error: 'content' is required — provide the complete file content.", 0

        full_path = (module_dir / rel_path).resolve()

        # Security: ensure path stays within module directory.
        try:
            full_path.relative_to(module_dir.resolve())
        except ValueError:
            return f"Error: path '{rel_path}' is outside the module directory.", 0

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            lines = content.count("\n") + 1
            return f"Written: {rel_path} ({lines} lines, {len(content):,} chars).", 0
        except Exception as e:
            return f"Error writing '{rel_path}': {e}", 0

    def _tool_run_command(self, args: dict) -> tuple[str, int]:
        """Run a configured test/lint command. Returns (output, 0)."""
        command = args.get("command", "")
        local_env = self.config.get("local_env", {})

        command_map = {
            "test": local_env.get("test_command", ""),
            "lint": local_env.get("lint_command", ""),
            "cache-clear": local_env.get("cache_clear", ""),
        }

        cmd_str = command_map.get(command, "")
        if not cmd_str:
            available = [k for k, v in command_map.items() if v]
            return f"Error: command '{command}' not configured. Available: {available}", 0

        logger.info("Agentic run_command: %s → %s", command, cmd_str)
        try:
            result = subprocess.run(
                cmd_str,
                shell=True,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = (result.stdout + result.stderr).strip()
            # Return last 8k chars — tail of output is most relevant (failures, summary).
            if len(output) > 8_000:
                output = "... (truncated)\n" + output[-8_000:]
            status = "PASS" if result.returncode == 0 else "FAIL"
            return f"[{status}] {command}\n{output}", 0
        except subprocess.TimeoutExpired:
            return f"[TIMEOUT] '{command}' timed out after 180s.", 0
        except Exception as e:
            return f"[ERROR] '{command}' failed: {e}", 0

    @staticmethod
    def _build_lean_prompt(work_item_title: str, story_context: str, module_path: str, analysis_hint: str = "") -> str:
        """Build a minimal prompt for Claude Code CLI — just the story essence.

        Claude Code CLI already knows how to explore codebases, read files,
        and make surgical edits. We don't need to send file trees, file contents,
        or elaborate instructions. Just the story — like typing in VS Code chat.

        If analysis_hint is provided (from the AI analysis stage), it's appended
        to guide the CLI toward the right files and approach — especially useful
        when PMs write vague stories without detailed descriptions.
        """
        # Extract just the description and acceptance criteria from context.
        # Strip the heavy parts (module structure, run history, dev notes).
        description = ""
        acceptance = ""
        for line in story_context.splitlines():
            if line.startswith("## Description"):
                # Capture until next ##
                idx = story_context.index(line)
                rest = story_context[idx + len(line):]
                end = rest.find("\n## ")
                description = rest[:end].strip() if end > 0 else rest.strip()
            elif line.startswith("## Acceptance Criteria"):
                idx = story_context.index(line)
                rest = story_context[idx + len(line):]
                end = rest.find("\n## ")
                acceptance = rest[:end].strip() if end > 0 else rest.strip()

        prompt = f"Implement this story in the module at '{module_path}':\n\n"
        prompt += f"**{work_item_title}**\n\n"
        if description and description != "None specified":
            prompt += f"{description[:1000]}\n\n"
        if acceptance and acceptance != "None specified":
            prompt += f"Acceptance Criteria:\n{acceptance[:500]}\n\n"
        if analysis_hint:
            prompt += f"Technical context (from analysis):\n{analysis_hint[:500]}\n\n"
        prompt += (
            "Make minimal, surgical changes. "
            "Read each file before modifying it. "
            "Do not create documentation files."
        )
        return prompt

    def _try_claude_code(self, story_context: str) -> dict | None:
        """Try using Claude Code CLI for implementation.

        Uses a lean prompt (~300 chars) — just the story essence. Claude Code
        CLI explores the codebase itself (like in VS Code). This avoids sending
        file trees, file contents, or run history — keeping tokens minimal.

        On rate limit (429): waits and retries up to 3 times instead of
        falling through to the expensive API path.
        """
        if not _command_exists("claude"):
            logger.debug("Claude Code CLI not available.")
            return None

        import time

        # Extract title from context (first line after "**Title:**").
        title = ""
        for line in story_context.splitlines():
            if "**Title:**" in line:
                title = line.split("**Title:**")[-1].strip()
                break

        prompt = self._build_lean_prompt(title, story_context, self.module_path, self.analysis_hint)
        logger.info("Claude Code CLI prompt: %d chars (lean%s).", len(prompt),
                     ", +analysis hint" if self.analysis_hint else "")

        if self.ticket_logger:
            self.ticket_logger.section("Claude Code CLI — Lean Prompt")
            self.ticket_logger.write(prompt)

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Implement story via Claude Code CLI (full filesystem access)",
                provider="claude-code-cli",
                model="local CLI",
                data_summary=[
                    ("Story context", f"Lean prompt ({len(prompt)} chars)"),
                    ("Module path", self.module_path),
                ],
                full_payload=prompt,
            )
            if not approved:
                return None

        # Retry on rate limit (429) — wait and try again instead of falling
        # through to the API path which will also hit the same rate limit.
        # In cli_only mode, retry more patiently with exponential backoff.
        cmd = ["claude", "-p"]
        if self.approval_mode == "auto":
            cmd.append("--dangerously-skip-permissions")
            logger.warning("Using --dangerously-skip-permissions (auto mode).")

        # Attach MCP config if available — gives Claude tool access to filesystem,
        # Azure DevOps, and git via MCP servers instead of blind text pipe.
        from src.mcp.config import get_mcp_config_path
        mcp_config = get_mcp_config_path()
        if mcp_config:
            cmd.extend(["--mcp-config", str(mcp_config)])
            logger.info("Claude CLI using MCP config: %s", mcp_config)

        max_retries = self.max_cli_retries if self.cli_only else 3
        base_wait = self.cli_retry_base_wait

        for attempt in range(max_retries):
            try:
                result = subprocess.run(
                    cmd,
                    input=prompt,
                    cwd=self.workspace_dir,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                output = (result.stdout or "").strip()
                stderr = (result.stderr or "").strip()

                # Success.
                if result.returncode == 0 and output:
                    logger.info("Claude Code CLI succeeded (attempt %d).", attempt + 1)
                    if self.ticket_logger:
                        self.ticket_logger.section("Claude Code CLI — Success")
                        self.ticket_logger.write(output[:5000])
                    return {
                        "success": True,
                        "method": "claude-code-cli",
                        "output": output[:5000],
                    }

                # Rate limit — wait and retry instead of falling through.
                combined = output + " " + stderr
                if "429" in combined or "rate_limit" in combined:
                    # Use retry-after header if present, otherwise exponential backoff.
                    header_wait = _parse_retry_after_from_text(combined, default=0)
                    backoff_wait = base_wait * (2 ** attempt)  # 60, 120, 240, ...
                    wait = max(header_wait, backoff_wait)
                    logger.warning(
                        "Claude Code CLI hit rate limit (attempt %d/%d). Waiting %ds...%s",
                        attempt + 1, max_retries, wait,
                        " (cli_only mode — will NOT fall through to API)" if self.cli_only else "",
                    )
                    if self.ticket_logger:
                        self.ticket_logger.kv(f"Rate limit (attempt {attempt + 1}/{max_retries})", f"waiting {wait}s")
                    time.sleep(wait)
                    continue

                # Other failure — log and give up.
                logger.warning(
                    "Claude Code CLI exit code %d (attempt %d). stderr: %s",
                    result.returncode, attempt + 1, stderr[:500],
                )
                if self.ticket_logger:
                    self.ticket_logger.section("Claude Code CLI — Failed")
                    self.ticket_logger.kv("Exit code", str(result.returncode))
                    self.ticket_logger.kv("stdout", output[:2000] or "(empty)")
                    self.ticket_logger.kv("stderr", stderr[:2000] or "(empty)")
                return None

            except subprocess.TimeoutExpired:
                logger.warning("Claude Code CLI timed out after 600s.")
                if self.ticket_logger:
                    self.ticket_logger.error("Claude Code CLI timed out after 600s.")
                return None
            except FileNotFoundError:
                logger.warning("Claude Code CLI not found in PATH.")
                return None

        logger.warning("Claude Code CLI: all %d rate-limit retries exhausted.", max_retries)
        return None

    def _try_codex_cli(self, story_context: str) -> dict | None:
        """Try using OpenAI Codex CLI for implementation."""
        if not _command_exists("codex"):
            logger.debug("Codex CLI not available.")
            return None

        logger.info("Using Codex CLI for implementation...")
        prompt = (
            f"Implement the following story in the codebase. "
            f"Module path: {self.module_path}\n\n"
            f"IMPORTANT: Before modifying any file, read its current contents first. "
            f"Make only surgical changes — add or modify what the story requires. "
            f"Do NOT rewrite or replace entire files.\n\n{story_context}"
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Implement story via Codex CLI (full filesystem access)",
                provider="codex-cli",
                model="local CLI",
                data_summary=[
                    ("Story context", "Work item title, description, acceptance criteria, comments"),
                    ("Module path", self.module_path),
                    ("⚠ Filesystem", "Codex CLI has full read/write access to workspace"),
                ],
                full_payload=prompt,
            )
            if not approved:
                return None

        try:
            # Use "suggest" mode for plan-review (Codex shows changes but doesn't apply).
            # Use "auto" mode only when approval_mode is "auto".
            codex_mode = "auto" if self.approval_mode == "auto" else "suggest"
            if codex_mode == "auto":
                logger.warning("Using Codex --approval-mode auto (auto mode).")
            result = subprocess.run(
                ["codex", "--approval-mode", codex_mode, prompt],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                return {
                    "success": True,
                    "method": "codex-cli",
                    "output": result.stdout[:5000],
                }
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("Codex CLI failed or timed out.")
        return None

    def _api_implementation(self, story_context: str) -> dict:
        """Use AI API to generate implementation plan and code."""
        logger.info("Using %s API for implementation analysis...", self.provider)

        system_prompt = self._load_system_prompt()
        module_summary = self._get_module_summary()

        full_prompt = (
            f"## Story Context\n\n{story_context}\n\n"
            f"## Module Structure\n\n{module_summary}\n\n"
            f"## Task\n\n"
            f"Analyze this story and provide:\n"
            f"1. Which files need to be modified or created\n"
            f"2. For NEW files: provide the full file content\n"
            f"3. For EXISTING files: provide ONLY the new code to add "
            f"(new functions, entry points, imports). Do NOT reproduce existing code.\n"
            f"4. Any configuration changes needed\n"
            f"5. Testing steps to verify the changes\n"
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Implement story via API",
                provider=self.provider,
                model=self.model,
                data_summary=[
                    ("Story context", "Work item title, description, acceptance criteria, comments"),
                    ("Module file tree", f"{self.module_path} — file names and directory structure"),
                    ("System prompt", "Development instructions"),
                ],
                full_payload=system_prompt + "\n\n" + full_prompt,
            )
            if not approved:
                return {
                    "success": False,
                    "method": f"{self.provider}-api",
                    "output": "User denied consent to send data.",
                }

        try:
            if self.provider == "anthropic":
                response = self._call_anthropic(system_prompt, full_prompt)
            elif self.provider == "copilot":
                response = self._call_copilot(system_prompt, full_prompt)
            else:
                response = self._call_openai(system_prompt, full_prompt)

            return {
                "success": True,
                "method": f"{self.provider}-api",
                "output": response,
            }
        except Exception as e:
            logger.error("AI API call failed: %s", e)
            return {
                "success": False,
                "method": f"{self.provider}-api",
                "output": str(e),
            }

    def _call_anthropic(self, system_prompt: str, user_prompt: str) -> str:
        """Call Anthropic API with retry on rate limit."""
        import time

        # Log AI input to per-ticket log.
        if self.ticket_logger:
            self.ticket_logger.ai_input("Anthropic _call_anthropic", system_prompt, user_prompt)

        client = anthropic.Anthropic()
        for attempt in range(4):
            try:
                message = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                self._record_usage(message, "implement")
                result_text = message.content[0].text
                # Log AI output to per-ticket log.
                if self.ticket_logger:
                    self.ticket_logger.ai_output("Anthropic _call_anthropic", result_text)
                return result_text
            except anthropic.RateLimitError as e:
                if attempt == 3:
                    from ..utils.rate_limit import record_rate_limit
                    record_rate_limit("anthropic", e)
                    raise
                wait = _parse_retry_after(e, default=60)
                logger.warning("Rate limit on _call_anthropic (attempt %d/4). Waiting %ds...", attempt + 1, wait)
                time.sleep(wait)
        raise RuntimeError("_call_anthropic: exhausted retries")

    def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        """Call OpenAI API."""
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        self._record_usage(response, "implement")
        return response.choices[0].message.content

    def _call_copilot(self, system_prompt: str, user_prompt: str) -> str:
        """Call GitHub Models API — OpenAI-compatible, uses `gh auth token`.

        Retries up to 3 times with exponential backoff for transient errors.
        """
        import time

        token = _get_github_token()
        client = openai.OpenAI(
            base_url="https://models.github.ai/inference/v1",
            api_key=token,
        )

        last_error = None
        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                self._record_usage(response, "implement")
                return response.choices[0].message.content
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as e:
                last_error = e
                wait = [5, 20, 60][attempt]  # Longer backoff for GitHub Models rate limits
                logger.warning("Copilot API attempt %d failed (%s), retrying in %ds...", attempt + 1, type(e).__name__, wait)
                time.sleep(wait)
            except openai.AuthenticationError:
                raise RuntimeError(
                    "GitHub token rejected by Models API. Run `gh auth login` or check GITHUB_TOKEN."
                )

        if isinstance(last_error, openai.RateLimitError):
            from src.utils.rate_limit import record_rate_limit, check_cooldown
            record_rate_limit("copilot", last_error)
            cooldown_msg = check_cooldown("copilot")
            if cooldown_msg:
                raise RuntimeError(cooldown_msg)
            raise RuntimeError(
                "⏳ GitHub Models API rate limit reached after 3 attempts.\n"
                "   Wait a few minutes and retry, or switch to a paid API key."
            )
        raise RuntimeError(f"Copilot API failed after 3 attempts: {last_error}")

    def _load_system_prompt(self) -> str:
        """Load the system prompt template, substituting framework-specific values."""
        from src.profiles import get_profile

        profile = get_profile(self.config)

        template_path = self.config.get("ai_agent", {}).get("system_prompt_template", "")
        if template_path:
            full_path = Path(__file__).parent.parent.parent / template_path
            if full_path.exists():
                template = full_path.read_text()
                # Substitute framework-specific placeholders.
                coding_standards = (
                    f"- Follow {profile['coding_standard']}\n"
                    f"- Target {profile['language']} {profile['language_version']}\n"
                    f"- Use dependency injection where possible\n"
                    f"- Add documentation comments for new public methods"
                )
                return template.replace("{coding_standards}", coding_standards)

        # Fallback: use profile-driven system prompt.
        return (
            f"{profile['system_prompt_prefix']} "
            f"When making changes, be precise and surgical — only change what's needed."
        )

    def _get_module_summary(self) -> str:
        """Get a summary of the module structure."""
        module_dir = self.workspace_dir / self.module_path
        if not module_dir.exists():
            return "Module directory not found."

        lines = []
        for p in sorted(module_dir.rglob("*")):
            if p.is_file() and not any(
                part in p.parts for part in ["vendor", "node_modules", ".git"]
            ):
                rel = p.relative_to(module_dir)
                lines.append(str(rel))

        # Limit to avoid token overflow.
        if len(lines) > 200:
            lines = lines[:200] + [f"... and {len(lines) - 200} more files"]

        return "```\n" + "\n".join(lines) + "\n```"

    def _call_ai(self, system_prompt: str, user_prompt: str) -> str:
        """Route AI call to the configured provider."""
        if self.provider == "anthropic":
            return self._call_anthropic(system_prompt, user_prompt)
        elif self.provider == "copilot":
            return self._call_copilot(system_prompt, user_prompt)
        else:
            return self._call_openai(system_prompt, user_prompt)

    def _select_relevant_files(self, story_context: str, module_summary: str) -> list[str]:
        """Select relevant files using Python keyword matching — zero API calls.

        Extracts keywords from the story context, scores every file in the module
        against those keywords, and returns the top matches.  This replaces a
        whole AI round-trip (saving ~1,500–3,000 tokens) while being deterministic
        and instantaneous.

        Falls back to returning all files if the module is small.
        """
        module_dir = self.workspace_dir / self.module_path
        if not module_dir.exists():
            return []

        # Extract keywords from story: words ≥4 chars, de-duplicated, lowercased.
        words = _re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{3,}\b', story_context)
        # Also pull camelCase/PascalCase parts (e.g. "UserService" → ["user", "service"]).
        for w in list(words):
            parts = _re.findall(r'[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z]|$)', w)
            words.extend(p.lower() for p in parts if len(p) >= 3)

        # Remove stop-words that match everything.
        stop = {
            "the", "and", "for", "this", "that", "with", "from", "will", "should",
            "have", "been", "when", "then", "also", "into", "over", "such", "each",
            "user", "story", "task", "file", "code", "change", "update", "add",
            "create", "new", "implement", "function", "method", "class", "test",
        }
        keywords = {w.lower() for w in words if w.lower() not in stop}

        # Collect all source files in the module.
        extensions = {".php", ".py", ".js", ".ts", ".tsx", ".jsx", ".java",
                      ".cs", ".yml", ".yaml", ".json", ".twig", ".html"}
        all_files: list[Path] = []
        for p in module_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in extensions:
                if not any(part.startswith(".") for part in p.parts):
                    if p.name not in ("composer.lock", "package-lock.json"):
                        all_files.append(p)

        if not all_files:
            return []

        # Return everything if the module is tiny (no need to filter).
        if len(all_files) <= 8:
            return [str(p.relative_to(module_dir)) for p in all_files]

        # Score each file: points for keyword hits in path/name, bonus for stem match.
        def score(p: Path) -> int:
            rel = str(p.relative_to(module_dir)).lower()
            stem = p.stem.lower()
            pts = 0
            for kw in keywords:
                if kw in rel:
                    pts += 3 if kw in stem else 1
            return pts

        scored = sorted(all_files, key=score, reverse=True)

        # Return the top 8 files that scored > 0, or top 4 if nothing scored.
        candidates = [p for p in scored if score(p) > 0][:8]
        if not candidates:
            candidates = scored[:4]

        result = [str(p.relative_to(module_dir)) for p in candidates]
        logger.info("Python file selection: %d candidate(s) from %d files → %s",
                    len(result), len(all_files), result)
        return result

    @staticmethod
    def _compress_source(content: str, suffix: str) -> str:
        """Remove blank lines and pure-comment lines to shrink token count.

        Keeps docstrings/block-comments intact (they carry semantic meaning).
        Targets 20–40% reduction on typical source files.
        Only applied to source code — config/YAML/JSON files are left as-is.
        """
        source_exts = {".php", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs"}
        if suffix.lower() not in source_exts:
            return content

        compressed: list[str] = []
        in_block_comment = False
        for line in content.splitlines():
            stripped = line.strip()

            # Track block comments (preserve them — they have semantic value).
            if "/*" in stripped:
                in_block_comment = True
            if in_block_comment:
                compressed.append(line)
                if "*/" in stripped:
                    in_block_comment = False
                continue

            # Drop blank lines.
            if not stripped:
                continue

            # Drop standalone single-line comments (// or # only lines).
            if stripped.startswith("//") or stripped.startswith("#"):
                continue

            compressed.append(line)

        return "\n".join(compressed)

    def _read_file_contents(self, file_paths: list[str]) -> tuple[str, dict[str, int]]:
        """Read selected files from disk within the token budget.

        Returns (formatted_content, {path: line_count}).
        Content is lightly compressed (blank/comment lines stripped) to
        reduce tokens sent to the API without losing semantic content.
        """
        module_dir = self.workspace_dir / self.module_path
        sections: list[str] = []
        line_counts: dict[str, int] = {}
        total_chars = 0

        for rel_path in file_paths:
            full_path = module_dir / rel_path
            if not full_path.exists() or not full_path.is_file():
                continue
            # Security: ensure path stays within module directory.
            try:
                full_path.resolve().relative_to(module_dir.resolve())
            except ValueError:
                logger.warning("Skipping path outside module dir: %s", rel_path)
                continue

            try:
                raw = full_path.read_text()
            except Exception as e:
                logger.warning("Could not read %s: %s", rel_path, e)
                continue

            content = self._compress_source(raw, full_path.suffix)
            if len(raw) != len(content):
                logger.debug("Compressed %s: %d → %d chars (%.0f%%)",
                             rel_path, len(raw), len(content),
                             100 * (1 - len(content) / len(raw)))

            if total_chars + len(content) > MAX_CONTEXT_CHARS:
                remaining = MAX_CONTEXT_CHARS - total_chars
                if remaining > 500:
                    content = content[:remaining] + "\n... (truncated)"
                else:
                    logger.info("Context budget reached — skipping remaining files.")
                    break

            lines = raw.count("\n") + 1  # Report original line count (not compressed).
            line_counts[rel_path] = lines
            sections.append(f"### {rel_path} ({lines} lines)\n```\n{content}\n```")
            total_chars += len(content)

        return "\n\n".join(sections), line_counts


def _get_github_token() -> str:
    """Get a GitHub token — prefers `gh auth token` (OAuth), falls back to GITHUB_TOKEN env var."""
    import os

    # Prefer gh CLI OAuth token (has broader access to GitHub Models)
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fall back to env var
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token

    raise RuntimeError(
        "No GitHub token found. Run `gh auth login` or set GITHUB_TOKEN env var."
    )


def _command_exists(cmd: str) -> bool:
    """Check if a command exists on the system."""
    try:
        result = subprocess.run(
            ["which", cmd], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
