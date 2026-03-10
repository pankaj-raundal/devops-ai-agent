"""Implementation agent — uses AI to implement story requirements."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import anthropic
import openai

logger = logging.getLogger("devops_ai_agent.implement")


class ImplementationAgent:
    """AI agent that reads story context and implements code changes."""

    def __init__(self, config: dict):
        self.config = config
        ai = config.get("ai_agent", {})
        self.provider = ai.get("provider", "anthropic")
        self.model = ai.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = ai.get("max_tokens", 8192)
        self.temperature = ai.get("temperature", 0.2)
        self.workspace_dir = Path(config["project"]["workspace_dir"])
        self.module_path = config["project"].get("module_path", "")

    def implement(self, story_context: str) -> dict:
        """Run the AI agent to implement the story.

        Returns a dict with:
          - success: bool
          - method: str (which method was used)
          - output: str (agent output/summary)
        """
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

    def _try_claude_code(self, story_context: str) -> dict | None:
        """Try using Claude Code CLI for implementation."""
        if not _command_exists("claude"):
            logger.debug("Claude Code CLI not available.")
            return None

        logger.info("Using Claude Code CLI for implementation...")
        prompt = (
            f"Read the following story context and implement the required changes "
            f"in the module at '{self.module_path}'. Follow Drupal coding standards. "
            f"Do not create documentation files unless asked.\n\n{story_context}"
        )

        try:
            result = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", "-m", prompt],
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
            f"Module path: {self.module_path}\n\n{story_context}"
        )

        try:
            result = subprocess.run(
                ["codex", "--approval-mode", "auto", prompt],
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
            f"2. The exact code changes needed (as diffs or full file contents)\n"
            f"3. Any configuration changes needed\n"
            f"4. Testing steps to verify the changes\n"
        )

        try:
            if self.provider == "anthropic":
                response = self._call_anthropic(system_prompt, full_prompt)
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

    def _load_system_prompt(self) -> str:
        """Load the system prompt template."""
        template_path = self.config.get("ai_agent", {}).get("system_prompt_template", "")
        if template_path:
            full_path = Path(__file__).parent.parent.parent / template_path
            if full_path.exists():
                return full_path.read_text()

        return (
            "You are an expert Drupal developer. You implement features and fix bugs "
            "in Drupal modules following Drupal coding standards (PSR-12 with Drupal "
            "conventions). You write clean, tested, secure PHP code compatible with PHP 8.4. "
            "When making changes, be precise and surgical — only change what's needed."
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


def _command_exists(cmd: str) -> bool:
    """Check if a command exists on the system."""
    try:
        result = subprocess.run(
            ["which", cmd], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
