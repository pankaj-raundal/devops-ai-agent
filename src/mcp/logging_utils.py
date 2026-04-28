"""Shared MCP logging setup — all MCP servers write tool calls to a log file.

Since MCP servers run as separate child processes (spawned by Claude CLI),
they cannot log to the main pipeline logger. Instead, they write to a
dedicated file in .dai/logs/ so you can see exactly what tools Claude called.

IMPORTANT: MCP servers communicate via stdio (JSON-RPC), so we MUST NOT
print to stdout. All logging goes to file only.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

# Log directory passed from pipeline via env var in .mcp.json.
_LOG_DIR = Path(os.environ.get("MCP_LOG_DIR", ".dai/logs"))
_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "unknown")
_SESSION_START = time.strftime("%Y%m%d_%H%M%S")

# Track tool calls for summary.
_tool_calls: list[dict] = []


def setup_mcp_file_logger(server_name: str) -> logging.Logger:
    """Set up a file-only logger for an MCP server.

    Creates a log file like: .dai/logs/mcp-filesystem-20260427_094500.log

    Args:
        server_name: Short name like 'filesystem', 'azure-devops', 'git'.

    Returns:
        A configured logger that writes to file only (never stdout).
    """
    global _SERVER_NAME
    _SERVER_NAME = server_name

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"mcp-{server_name}-{_SESSION_START}.log"

    logger = logging.getLogger(f"mcp.{server_name}")
    logger.setLevel(logging.DEBUG)

    # Remove any existing handlers (avoid duplicates on re-init).
    logger.handlers.clear()

    # File handler only — NEVER add a StreamHandler (would corrupt stdio).
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(fh)

    logger.info("=== MCP Server '%s' started ===", server_name)
    logger.info("Log file: %s", log_file)

    return logger


def log_tool_call(logger: logging.Logger, tool_name: str, args: dict, result_preview: str = "") -> None:
    """Log a tool call with arguments and result preview.

    Args:
        logger: The MCP file logger.
        tool_name: Name of the tool (e.g. 'read_file', 'git_diff').
        args: Dict of arguments passed by Claude.
        result_preview: First ~200 chars of the result (for debugging).
    """
    _tool_calls.append({
        "tool": tool_name,
        "args": args,
        "result_len": len(result_preview),
        "time": time.strftime("%H:%M:%S"),
    })

    # Format args as key=value pairs.
    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else "(no args)"
    logger.info("TOOL CALL: %s(%s)", tool_name, args_str)

    if result_preview:
        # Truncate long results for the log.
        preview = result_preview[:300].replace("\n", "\\n")
        logger.info("  → result (%d chars): %s%s", len(result_preview), preview,
                     "..." if len(result_preview) > 300 else "")


def get_session_summary() -> str:
    """Return a summary of all tool calls in this MCP session."""
    if not _tool_calls:
        return f"MCP server '{_SERVER_NAME}': no tool calls received."

    lines = [f"MCP server '{_SERVER_NAME}': {len(_tool_calls)} tool call(s):"]
    for i, call in enumerate(_tool_calls, 1):
        lines.append(f"  {i}. [{call['time']}] {call['tool']}({_format_args(call['args'])}) → {call['result_len']} chars")
    return "\n".join(lines)


def _format_args(args: dict) -> str:
    """Format args dict as short string."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        v_str = repr(v)
        if len(v_str) > 50:
            v_str = v_str[:47] + "..."
        parts.append(f"{k}={v_str}")
    return ", ".join(parts)
