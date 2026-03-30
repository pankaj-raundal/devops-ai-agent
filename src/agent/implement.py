"""Implementation agent — uses AI to implement story requirements."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import anthropic
import openai

logger = logging.getLogger("devops_ai_agent.implement")

# Maximum characters of file content to include in AI context (~7500 tokens).
MAX_CONTEXT_CHARS = 30_000


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
        self.workspace_dir = Path(config["project"]["workspace_dir"])
        self.module_path = config["project"].get("module_path", "")

    def implement(self, story_context: str) -> dict:
        """Run the AI agent to implement the story.

        Returns a dict with:
          - success: bool
          - method: str (which method was used)
          - output: str (agent output/summary)
        """
        if self.approval_mode == "auto":
            return self._implement_auto(story_context)
        # Default: plan-review mode — generates a plan only, does NOT write files.
        return self._implement_plan(story_context)

    def _implement_auto(self, story_context: str) -> dict:
        """Auto mode — direct AI execution (original behavior)."""
        # Strategy 1: Try Claude Code CLI (best for file-level changes).
        result = self._try_claude_code(story_context)
        if result:
            return result

        # Strategy 2: Try Codex CLI.
        result = self._try_codex_cli(story_context)
        if result:
            return result

        # Strategy 3: Fall back to API-based implementation.
        return self._api_implementation(story_context)

    def _implement_plan(self, story_context: str) -> dict:
        """Plan-review mode — two strategies based on tool availability.

        Strategy 1 (CLI tools available): Let Claude Code / Codex read files
        from the filesystem and make surgical edits directly. No append needed.

        Strategy 2 (API fallback): AI has no filesystem access. It returns
        only the new code to add; the pipeline appends it to existing files.

        Either way, a plan is returned for human review. The pipeline is
        responsible for getting approval and calling apply_plan().
        """
        # Try CLI tools first — they can read files and make surgical edits.
        result = self._try_claude_code_plan(story_context)
        if result:
            return result

        result = self._try_codex_cli_plan(story_context)
        if result:
            return result

        # Fallback: API call — AI cannot read files, uses append strategy.
        return self._api_plan(story_context)

    def _try_claude_code_plan(self, story_context: str) -> dict | None:
        """Plan-review via Claude Code CLI — CLI reads files itself."""
        if not _command_exists("claude"):
            return None

        from .plan import PLAN_JSON_SCHEMA, parse_plan_response

        logger.info("Using Claude Code CLI for plan generation (reads files directly)...")
        prompt = (
            f"Read the following story context and produce an implementation plan "
            f"for the module at '{self.module_path}'.\n\n"
            f"IMPORTANT: You have full filesystem access. READ each file you plan to "
            f"modify so you understand its current contents. Then produce a plan with "
            f"ONLY the changes needed — do NOT include unchanged existing code.\n\n"
            f"For each file_change with action='modify', set merge_strategy='replace' "
            f"and provide the COMPLETE updated file (since you can read the originals).\n"
            f"For action='create', provide full file content with merge_strategy='replace'.\n\n"
            f"Return ONLY valid JSON matching this schema:\n"
            f"```json\n{PLAN_JSON_SCHEMA}\n```\n\n"
            f"## Story Context\n\n{story_context}"
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Generate plan via Claude Code CLI (reads files from filesystem)",
                provider="claude-code-cli",
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
            cmd = ["claude", "--print", "-m", prompt]
            result = subprocess.run(
                cmd, cwd=self.workspace_dir,
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0 and result.stdout.strip():
                plan = parse_plan_response(result.stdout)
                # Mark all file changes as CLI-sourced (replace strategy is safe).
                for fc in plan.file_changes:
                    if fc.action == "modify":
                        fc.merge_strategy = "replace"
                return {
                    "success": True,
                    "method": "claude-code-plan",
                    "output": plan.to_markdown(),
                    "plan": plan,
                }
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

    def _try_claude_code(self, story_context: str) -> dict | None:
        """Try using Claude Code CLI for implementation."""
        if not _command_exists("claude"):
            logger.debug("Claude Code CLI not available.")
            return None

        logger.info("Using Claude Code CLI for implementation...")
        prompt = (
            f"Read the following story context and implement the required changes "
            f"in the project at '{self.module_path}'. Follow project coding standards.\n\n"
            f"IMPORTANT: Before modifying any file, READ its current contents first. "
            f"Make only surgical changes — add or modify what the story requires. "
            f"Do NOT rewrite or replace entire files. "
            f"Do not create documentation files unless asked.\n\n{story_context}"
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="Implement story via Claude Code CLI (full filesystem access)",
                provider="claude-code-cli",
                model="local CLI",
                data_summary=[
                    ("Story context", "Work item title, description, acceptance criteria, comments"),
                    ("Module path", self.module_path),
                    ("⚠ Filesystem", "Claude Code CLI has full read/write access to workspace"),
                ],
                full_payload=prompt,
            )
            if not approved:
                return None

        try:
            # --print outputs to stdout without interactive mode.
            # Only use --dangerously-skip-permissions in auto mode; otherwise
            # Claude CLI will prompt for each file change (safer).
            cmd = ["claude", "--print", "-m", prompt]
            if self.approval_mode == "auto":
                cmd.insert(2, "--dangerously-skip-permissions")
                logger.warning("Using --dangerously-skip-permissions (auto mode).")
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                return {
                    "success": True,
                    "method": "claude-code-cli",
                    "output": result.stdout[:5000],
                }
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("Claude Code CLI failed or timed out.")
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
        """Call Anthropic API."""
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return message.content[0].text

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
                return response.choices[0].message.content
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as e:
                last_error = e
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                logger.warning("Copilot API attempt %d failed (%s), retrying in %ds...", attempt + 1, type(e).__name__, wait)
                time.sleep(wait)
            except openai.AuthenticationError:
                raise RuntimeError(
                    "GitHub token rejected by Models API. Run `gh auth login` or check GITHUB_TOKEN."
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
        """Pass 1 of context injection: ask AI which files to read."""
        prompt = (
            "Given this story and module file tree, return a JSON array of file paths "
            "that you would need to READ to implement this story correctly.\n\n"
            "Include:\n"
            "- Files you would MODIFY\n"
            "- Files you need to UNDERSTAND (interfaces, base classes, services)\n"
            "- Configuration files that might need changes\n\n"
            "Return ONLY a JSON array of relative file paths, nothing else. Example:\n"
            '["src/MyService.php", "config/services.yml"]\n\n'
            f"## Story\n\n{story_context}\n\n"
            f"## Module Files\n\n{module_summary}"
        )

        system = "You are a file selection assistant. Return ONLY a JSON array of file paths. No explanation."

        try:
            response = self._call_ai(system, prompt)

            import json as _json
            import re as _re
            text = response.strip()
            fence = _re.search(r'```(?:json)?\s*\n(.*?)```', text, _re.DOTALL)
            if fence:
                text = fence.group(1).strip()
            files = _json.loads(text)
            if isinstance(files, list):
                return [f for f in files if isinstance(f, str)][:20]
        except Exception as e:
            logger.warning("File selection pass failed: %s — will use append strategy", e)

        return []

    def _read_file_contents(self, file_paths: list[str]) -> tuple[str, dict[str, int]]:
        """Read selected files from disk within the token budget.

        Returns (formatted_content, {path: line_count}).
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
                content = full_path.read_text()
            except Exception as e:
                logger.warning("Could not read %s: %s", rel_path, e)
                continue

            if total_chars + len(content) > MAX_CONTEXT_CHARS:
                remaining = MAX_CONTEXT_CHARS - total_chars
                if remaining > 500:
                    content = content[:remaining] + "\n... (truncated)"
                else:
                    logger.info("Context budget reached — skipping remaining files.")
                    break

            lines = content.count("\n") + 1
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
