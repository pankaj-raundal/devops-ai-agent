"""Per-ticket logging — creates a dedicated log file for each story/work item.

Each run produces a file like:
  .dai/logs/1636226_2026-04-09_14-30-22.log

The log captures everything the pipeline does for this ticket:
  - Story context sent to AI
  - AI input (system prompt + user prompt)
  - AI output (plan JSON, tool calls, final summary)
  - Tool call details (read_file, write_file, run_command)
  - Test/lint results
  - Review verdict and findings
  - Git operations (branch, commit, push)
  - Token usage per API call
  - Errors and rate limit events
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.ticket_logger")

_DATA_DIR = Path(".dai")
_LOG_DIR = _DATA_DIR / "logs"


class TicketLogger:
    """Dedicated logger for a single ticket/story run.

    Usage:
        tl = TicketLogger(work_item_id=1636226)
        tl.section("Story Context")
        tl.write(story_context)
        tl.section("AI Input")
        tl.write(system_prompt)
        ...
        tl.close()
    """

    def __init__(self, work_item_id: int | str):
        self.work_item_id = work_item_id
        self._start_time = time.time()

        # Create log directory in the devops-ai-agent repo (where the CLI runs from).
        log_dir = Path.cwd() / _LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)

        # Create log file with timestamp.
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._filepath = log_dir / f"{work_item_id}_{ts}.log"
        self._file = open(self._filepath, "w", encoding="utf-8")

        # Write header.
        self._file.write(f"{'=' * 80}\n")
        self._file.write(f"DevOps AI Agent — Ticket #{work_item_id}\n")
        self._file.write(f"Started: {datetime.now().isoformat()}\n")
        self._file.write(f"{'=' * 80}\n\n")
        self._file.flush()

        logger.info("Ticket log: %s", self._filepath)

    @property
    def filepath(self) -> Path:
        return self._filepath

    def section(self, title: str) -> None:
        """Write a section header."""
        elapsed = time.time() - self._start_time
        self._file.write(f"\n{'─' * 80}\n")
        self._file.write(f"[{elapsed:7.1f}s] {title}\n")
        self._file.write(f"{'─' * 80}\n\n")
        self._file.flush()

    def write(self, content: str) -> None:
        """Write content to the log."""
        self._file.write(content)
        if not content.endswith("\n"):
            self._file.write("\n")
        self._file.flush()

    def kv(self, key: str, value: str) -> None:
        """Write a key-value pair."""
        self._file.write(f"  {key}: {value}\n")
        self._file.flush()

    def stage(self, name: str, status: str, detail: str = "") -> None:
        """Log a stage transition."""
        elapsed = time.time() - self._start_time
        icon = {"pass": "+", "fail": "X", "running": "~", "skipped": "-"}.get(status, "?")
        line = f"[{elapsed:7.1f}s] [{icon}] {name}: {status}"
        if detail:
            line += f" — {detail}"
        self._file.write(line + "\n")
        self._file.flush()

    def ai_input(self, label: str, system_prompt: str, user_prompt: str) -> None:
        """Log the full input sent to an AI call."""
        self.section(f"AI Input — {label}")
        self._file.write(f"--- System Prompt ({len(system_prompt):,} chars) ---\n")
        self._file.write(system_prompt + "\n\n")
        self._file.write(f"--- User Prompt ({len(user_prompt):,} chars) ---\n")
        self._file.write(user_prompt + "\n")
        self._file.flush()

    def ai_output(self, label: str, output: str) -> None:
        """Log AI response output."""
        self.section(f"AI Output — {label}")
        self._file.write(f"({len(output):,} chars)\n\n")
        self._file.write(output + "\n")
        self._file.flush()

    def tool_call(self, name: str, args: dict, result: str, chars_used: int = 0) -> None:
        """Log a tool call and its result."""
        elapsed = time.time() - self._start_time
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "content")
        # Truncate content arg for write_file (log the length, not the full content).
        if "content" in args:
            args_str += f", content=<{len(args['content']):,} chars>"
        self._file.write(f"[{elapsed:7.1f}s] TOOL: {name}({args_str})\n")
        # Truncate very long results.
        result_preview = result[:2000] + "..." if len(result) > 2000 else result
        self._file.write(f"  Result ({len(result):,} chars): {result_preview}\n")
        if chars_used:
            self._file.write(f"  Budget used: {chars_used:,} chars\n")
        self._file.flush()

    def token_usage(self, stage: str, provider: str, model: str,
                    prompt_tokens: int, completion_tokens: int) -> None:
        """Log token usage for an API call."""
        total = prompt_tokens + completion_tokens
        elapsed = time.time() - self._start_time
        self._file.write(
            f"[{elapsed:7.1f}s] TOKENS: {stage} | {provider}/{model} | "
            f"prompt={prompt_tokens:,} completion={completion_tokens:,} total={total:,}\n"
        )
        self._file.flush()

    def error(self, message: str) -> None:
        """Log an error."""
        elapsed = time.time() - self._start_time
        self._file.write(f"[{elapsed:7.1f}s] ERROR: {message}\n")
        self._file.flush()

    def close(self) -> None:
        """Finalize the log file."""
        elapsed = time.time() - self._start_time
        self._file.write(f"\n{'=' * 80}\n")
        self._file.write(f"Completed in {elapsed:.1f}s\n")
        self._file.write(f"{'=' * 80}\n")
        self._file.close()
        logger.info("Ticket log saved: %s (%.1fs)", self._filepath, elapsed)
