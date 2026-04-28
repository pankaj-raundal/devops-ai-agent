"""MCP configuration generator — creates .mcp.json for Claude CLI sessions.

Called by pipeline before invoking Claude CLI with --mcp-config.
Generates a .mcp.json file with correct MODULE_PATH, workspace, and env vars
so Claude can use the MCP servers defined in src/mcp/.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.mcp.config")

# Path to the generated MCP config file (in the devops-ai-agent repo root).
_REPO_ROOT = Path(__file__).parent.parent.parent
MCP_CONFIG_PATH = _REPO_ROOT / ".mcp.json"


def generate_mcp_config(
    module_path: str,
    workspace_dir: str,
    config: dict,
) -> Path:
    """Generate .mcp.json with correct paths and env vars for this run.

    Args:
        module_path: Absolute path to the target module (e.g. /home/user/project/web/modules/custom/my_module).
        workspace_dir: Absolute path to the project workspace root.
        config: The full pipeline config dict.

    Returns:
        Path to the generated .mcp.json file.
    """
    ado = config.get("azure_devops", {})
    project_cfg = config.get("project", {})

    # MCP servers log tool calls to this directory.
    log_dir = str(_REPO_ROOT / ".dai" / "logs")

    mcp_config = {
        "mcpServers": {
            "filesystem": {
                "command": "python",
                "args": ["-m", "src.mcp.filesystem_server"],
                "cwd": str(_REPO_ROOT),
                "env": {
                    "MODULE_PATH": str(module_path),
                    "WORKSPACE_PATH": str(workspace_dir),
                    "MCP_LOG_DIR": log_dir,
                },
            },
            "azure-devops": {
                "command": "python",
                "args": ["-m", "src.mcp.azure_devops_server"],
                "cwd": str(_REPO_ROOT),
                "env": {
                    "AZURE_DEVOPS_ORG": ado.get("organization", ""),
                    "AZURE_DEVOPS_PROJECT": ado.get("project", ""),
                    "MCP_LOG_DIR": log_dir,
                },
            },
            "git": {
                "command": "python",
                "args": ["-m", "src.mcp.git_server"],
                "cwd": str(_REPO_ROOT),
                "env": {
                    "GIT_WORKSPACE": str(workspace_dir),
                    "GIT_BASE_BRANCH": project_cfg.get("base_branch", "master"),
                    "MCP_LOG_DIR": log_dir,
                },
            },
        }
    }

    MCP_CONFIG_PATH.write_text(json.dumps(mcp_config, indent=2) + "\n")
    logger.info("Generated MCP config: %s", MCP_CONFIG_PATH)
    return MCP_CONFIG_PATH


def get_mcp_config_path() -> Path | None:
    """Return the MCP config path if it exists, else None."""
    if MCP_CONFIG_PATH.exists():
        return MCP_CONFIG_PATH
    return None


def cleanup_mcp_config() -> None:
    """Remove the generated .mcp.json file."""
    if MCP_CONFIG_PATH.exists():
        MCP_CONFIG_PATH.unlink()
        logger.debug("Cleaned up MCP config: %s", MCP_CONFIG_PATH)
