"""Git MCP Server — gives Claude read access to git status, diffs, and logs.

Tools:
  - git_status: Show working tree status
  - git_diff: Show diff vs base branch
  - git_log: Show recent commit log
  - get_changed_files: List files changed vs base branch

Security:
  - All tools are READ-ONLY (no commit, push, checkout, or reset)
  - Diff output truncated to prevent token explosion
  - Workspace path resolved from env var

Usage:
  GIT_WORKSPACE=/path/to/project GIT_BASE_BRANCH=master \
    python -m src.mcp.git_server
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from src.mcp.logging_utils import log_tool_call, setup_mcp_file_logger

logger = logging.getLogger("devops_ai_agent.mcp.git")

WORKSPACE = Path(os.environ.get("GIT_WORKSPACE", ".")).resolve()
BASE_BRANCH = os.environ.get("GIT_BASE_BRANCH", "master")

# Max chars for diff output to prevent token explosion.
MAX_DIFF_CHARS = 30_000

# Set up file-based logging for this MCP server.
_mcp_logger = setup_mcp_file_logger("git")
_mcp_logger.info("WORKSPACE=%s, BASE_BRANCH=%s", WORKSPACE, BASE_BRANCH)

mcp = FastMCP(
    "devops-ai-agent-git",
    instructions=(
        "Git tools for inspecting the current repository state. "
        "Use git_diff to see changes made so far. "
        "Use git_status to check for uncommitted files. "
        "All tools are read-only — no commits or pushes."
    ),
)


def _run_git(*args: str) -> str:
    """Run a git command in the workspace and return stdout."""
    cmd = ["git"] + list(args)
    logger.debug("git %s", " ".join(args))
    result = subprocess.run(
        cmd,
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Some git commands return non-zero for valid "no results" (e.g., empty diff).
        if not stderr:
            return result.stdout.strip()
        raise RuntimeError(f"git {args[0]} failed: {stderr[:500]}")
    return result.stdout.strip()


@mcp.tool()
def git_status() -> str:
    """Show the working tree status (short format).

    Returns:
        Git status output showing modified, added, and untracked files.
    """
    try:
        output = _run_git("status", "--short")
        if not output:
            result = "Working tree is clean — no changes."
        else:
            result = f"Git status:\n{output}"
        log_tool_call(_mcp_logger, "git_status", {}, result)
        return result
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def git_diff(base: str = "") -> str:
    """Show the diff of all changes vs the base branch.

    Args:
        base: Base branch to diff against (default: configured base branch).

    Returns:
        Unified diff output (truncated to ~30,000 chars if large).
    """
    if not base:
        base = BASE_BRANCH

    try:
        output = _run_git("diff", base, "--", ".")
        if not output:
            result = f"No diff vs {base} — no changes detected."
        else:
            if len(output) > MAX_DIFF_CHARS:
                output = output[:MAX_DIFF_CHARS] + f"\n\n[Diff truncated at {MAX_DIFF_CHARS:,} chars]"
            result = output
        log_tool_call(_mcp_logger, "git_diff", {"base": base}, result)
        return result
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def git_log(count: int = 10) -> str:
    """Show recent commit log (oneline format).

    Args:
        count: Number of recent commits to show (default 10, max 50).

    Returns:
        One-line commit log.
    """
    count = min(max(1, count), 50)

    try:
        output = _run_git("log", "--oneline", f"-n{count}")
        if not output:
            result = "No commits found."
        else:
            result = f"Recent {count} commits:\n{output}"
        log_tool_call(_mcp_logger, "git_log", {"count": count}, result)
        return result
    except RuntimeError as e:
        return f"Error: {e}"


@mcp.tool()
def get_changed_files(base: str = "") -> str:
    """List files changed compared to the base branch.

    Args:
        base: Base branch to compare against (default: configured base branch).

    Returns:
        List of changed file paths, one per line.
    """
    if not base:
        base = BASE_BRANCH

    try:
        output = _run_git("diff", "--name-only", base)
        if not output:
            return f"No files changed vs {base}."
        files = [f for f in output.splitlines() if f.strip()]
        result = f"Changed files vs {base} ({len(files)} files):\n" + "\n".join(f"  {f}" for f in files)
        log_tool_call(_mcp_logger, "get_changed_files", {"base": base}, result)
        return result
    except RuntimeError as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
