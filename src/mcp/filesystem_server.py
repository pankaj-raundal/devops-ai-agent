"""Filesystem MCP Server — gives Claude sandboxed file access to the target module.

Tools:
  - read_file: Read file content (with optional line range)
  - list_directory: List files/dirs in a directory
  - write_file: Write/overwrite a file (creates backup)
  - run_command: Run whitelisted commands (test, lint, cache-clear)

Security:
  - All paths sandboxed to MODULE_PATH (no escape via ../ or symlinks)
  - write_file creates .bak backup before overwriting
  - run_command only allows whitelisted operations
  - Character budget tracking prevents unbounded reads

Usage:
  MODULE_PATH=/path/to/module python -m src.mcp.filesystem_server
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from src.mcp.logging_utils import log_tool_call, setup_mcp_file_logger

logger = logging.getLogger("devops_ai_agent.mcp.filesystem")

# Budget: max chars Claude can read per session to prevent runaway reads.
MAX_READ_CHARS = 200_000
_chars_read = 0

# Resolve the module root from env var (set by pipeline via .mcp.json).
MODULE_PATH = Path(os.environ.get("MODULE_PATH", ".")).resolve()
WORKSPACE_PATH = Path(os.environ.get("WORKSPACE_PATH", MODULE_PATH)).resolve()

# Set up file-based logging for this MCP server.
_mcp_logger = setup_mcp_file_logger("filesystem")
_mcp_logger.info("MODULE_PATH=%s", MODULE_PATH)
_mcp_logger.info("WORKSPACE_PATH=%s", WORKSPACE_PATH)

mcp = FastMCP(
    "devops-ai-agent-filesystem",
    instructions=(
        "Filesystem tools for the target Drupal/PHP module. "
        "All paths are relative to the module root. "
        "Read files before modifying them. Write complete file contents."
    ),
)


def _resolve_safe(relative_path: str) -> Path:
    """Resolve a relative path inside MODULE_PATH. Raise on sandbox escape."""
    # Normalize and resolve.
    target = (MODULE_PATH / relative_path).resolve()
    # Ensure it's within the module root.
    if not str(target).startswith(str(MODULE_PATH)):
        raise ValueError(f"Path escapes module sandbox: {relative_path}")
    return target


@mcp.tool()
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file from the project module.

    Args:
        path: File path relative to the module root.
        start_line: Start line (1-based). 0 = from beginning.
        end_line: End line (1-based, inclusive). 0 = to end.

    Returns:
        File content (or the requested line range).
    """
    global _chars_read

    if _chars_read >= MAX_READ_CHARS:
        return f"[Budget exceeded: already read {_chars_read:,} chars. Write your changes now.]"

    target = _resolve_safe(path)
    if not target.is_file():
        return f"Error: File not found: {path}"

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading {path}: {e}"

    # Apply line range if specified.
    if start_line > 0 or end_line > 0:
        lines = content.splitlines(keepends=True)
        start = max(0, start_line - 1)
        end = end_line if end_line > 0 else len(lines)
        content = "".join(lines[start:end])

    _chars_read += len(content)

    if _chars_read >= MAX_READ_CHARS:
        remaining = MAX_READ_CHARS - (_chars_read - len(content))
        content = content[:remaining]
        content += f"\n\n[Truncated: read budget reached ({MAX_READ_CHARS:,} chars)]"

    log_tool_call(_mcp_logger, "read_file", {"path": path, "start_line": start_line, "end_line": end_line, "budget_used": _chars_read}, content)
    return content


@mcp.tool()
def list_directory(path: str = ".") -> str:
    """List files and subdirectories in a directory within the module.

    Args:
        path: Directory path relative to module root. Use '.' for root.

    Returns:
        Listing with file names and sizes.
    """
    target = _resolve_safe(path)
    if not target.is_dir():
        return f"Error: Not a directory: {path}"

    entries = []
    try:
        for item in sorted(target.iterdir()):
            rel = item.relative_to(MODULE_PATH)
            if item.is_dir():
                entries.append(f"  {rel}/")
            else:
                size = item.stat().st_size
                if size < 1024:
                    size_str = f"{size} B"
                elif size < 1024 * 1024:
                    size_str = f"{size / 1024:.1f} KB"
                else:
                    size_str = f"{size / (1024 * 1024):.1f} MB"
                entries.append(f"  {rel}  ({size_str})")
    except PermissionError:
        return f"Error: Permission denied: {path}"

    if not entries:
        return f"(empty directory: {path})"

    result = f"Contents of {path}:\n" + "\n".join(entries)
    log_tool_call(_mcp_logger, "list_directory", {"path": path}, result)
    return result


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write or overwrite a file in the project module.

    Creates a .bak backup before overwriting existing files.
    Always provide the COMPLETE file content — never partial.

    Args:
        path: File path relative to module root.
        content: Complete file content to write.

    Returns:
        Confirmation message.
    """
    target = _resolve_safe(path)

    # Create parent directories if needed.
    target.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing file.
    if target.exists():
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        logger.info("Backed up %s → %s", path, backup.name)

    try:
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"Error writing {path}: {e}"

    logger.info("Wrote %s (%d chars)", path, len(content))
    result = f"Successfully wrote {path} ({len(content)} chars)"
    log_tool_call(_mcp_logger, "write_file", {"path": path, "content_len": len(content)}, result)
    return result


# Whitelisted commands that run_command accepts.
ALLOWED_COMMANDS = {
    "test": "Run the project test suite",
    "lint": "Run the linter / code style checker",
    "cache-clear": "Clear the application cache",
}


@mcp.tool()
def run_command(command: str) -> str:
    """Run a whitelisted command in the project workspace.

    Args:
        command: One of 'test', 'lint', 'cache-clear'.

    Returns:
        Command output (stdout + stderr, truncated to 10,000 chars).
    """
    if command not in ALLOWED_COMMANDS:
        return f"Error: Command '{command}' not allowed. Allowed: {', '.join(ALLOWED_COMMANDS)}"

    # Map logical commands to actual shell commands based on project type.
    # These match what TestRunner uses in src/reviewer/test_runner.py.
    cmd_map = {
        "test": _get_test_command(),
        "lint": _get_lint_command(),
        "cache-clear": _get_cache_clear_command(),
    }

    shell_cmd = cmd_map[command]
    logger.info("Running command: %s → %s", command, shell_cmd)

    try:
        result = subprocess.run(
            shell_cmd,
            shell=True,
            cwd=str(WORKSPACE_PATH),
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + "\n" + result.stderr
        output = output.strip()

        # Truncate long output.
        if len(output) > 10_000:
            output = output[:10_000] + "\n\n[Output truncated at 10,000 chars]"

        status = "succeeded" if result.returncode == 0 else f"failed (exit {result.returncode})"
        result_str = f"Command '{command}' {status}:\n\n{output}"
        log_tool_call(_mcp_logger, "run_command", {"command": command, "shell_cmd": shell_cmd}, result_str)
        return result_str

    except subprocess.TimeoutExpired:
        return f"Command '{command}' timed out after 300s."
    except Exception as e:
        return f"Error running '{command}': {e}"


def _get_test_command() -> str:
    """Determine the test command from env or defaults."""
    custom = os.environ.get("TEST_COMMAND")
    if custom:
        return custom
    # Default: PHPUnit for Drupal projects.
    if (WORKSPACE_PATH / "phpunit.xml").exists() or (WORKSPACE_PATH / "phpunit.xml.dist").exists():
        return "vendor/bin/phpunit"
    if (WORKSPACE_PATH / "pytest.ini").exists() or (WORKSPACE_PATH / "pyproject.toml").exists():
        return "pytest -v"
    return "echo 'No test runner detected'"


def _get_lint_command() -> str:
    """Determine the lint command from env or defaults."""
    custom = os.environ.get("LINT_COMMAND")
    if custom:
        return custom
    if (WORKSPACE_PATH / "phpcs.xml").exists() or (WORKSPACE_PATH / "phpcs.xml.dist").exists():
        return "vendor/bin/phpcs"
    if (WORKSPACE_PATH / "pyproject.toml").exists():
        return "ruff check ."
    return "echo 'No linter detected'"


def _get_cache_clear_command() -> str:
    """Determine the cache clear command from env or defaults."""
    custom = os.environ.get("CACHE_CLEAR_COMMAND")
    if custom:
        return custom
    # Default: Drupal cache clear via drush.
    return "drush cr 2>/dev/null || echo 'Cache clear not available'"


def reset_budget() -> None:
    """Reset the read budget counter (for testing)."""
    global _chars_read
    _chars_read = 0


if __name__ == "__main__":
    mcp.run()
