"""Azure DevOps MCP Server — gives Claude live access to ADO work items.

Tools:
  - get_work_item: Fetch full details for a work item by ID
  - query_work_items: Run a WIQL query and return matching items
  - add_comment: Post a comment to a work item (rate-limited)

Security:
  - Read operations are unrestricted
  - add_comment capped at 5 per session to prevent spam
  - Uses existing az CLI auth (AZURE_DEVOPS_PAT env var)

Usage:
  AZURE_DEVOPS_ORG=https://dev.azure.com/org AZURE_DEVOPS_PROJECT=proj \
    python -m src.mcp.azure_devops_server
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from mcp.server.fastmcp import FastMCP

from src.mcp.logging_utils import log_tool_call, setup_mcp_file_logger

logger = logging.getLogger("devops_ai_agent.mcp.azure_devops")

ORG = os.environ.get("AZURE_DEVOPS_ORG", "")
PROJECT = os.environ.get("AZURE_DEVOPS_PROJECT", "")

# Rate limit: max comments per MCP session.
_MAX_COMMENTS = 5
_comments_posted = 0

# Set up file-based logging for this MCP server.
_mcp_logger = setup_mcp_file_logger("azure-devops")
_mcp_logger.info("ORG=%s, PROJECT=%s", ORG, PROJECT)

mcp = FastMCP(
    "devops-ai-agent-azure-devops",
    instructions=(
        "Azure DevOps tools for querying work items, reading story details, "
        "and posting comments. Use get_work_item to look up related stories "
        "referenced in descriptions."
    ),
)


def _run_az_json(args: list[str]) -> dict | list:
    """Run an az CLI command and return parsed JSON."""
    cmd = ["az"] + args + ["--output", "json"]
    logger.debug("az %s", " ".join(args))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"az command failed: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@mcp.tool()
def get_work_item(work_item_id: int) -> str:
    """Fetch full details for an Azure DevOps work item.

    Args:
        work_item_id: The numeric work item ID (e.g. 1639102).

    Returns:
        Markdown-formatted work item details.
    """
    if not ORG:
        return "Error: AZURE_DEVOPS_ORG not set."

    try:
        data = _run_az_json([
            "boards", "work-item", "show",
            "--id", str(work_item_id),
            "--org", ORG,
        ])
    except (RuntimeError, json.JSONDecodeError) as e:
        return f"Error fetching work item #{work_item_id}: {e}"

    fields = data.get("fields", {})
    title = fields.get("System.Title", "Unknown")
    state = fields.get("System.State", "Unknown")
    wi_type = fields.get("System.WorkItemType", "Unknown")
    desc = _strip_html(fields.get("System.Description") or "No description")
    criteria = _strip_html(
        fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or "None specified"
    )
    tags = fields.get("System.Tags", "")

    result = (
        f"# #{work_item_id}: {title}\n\n"
        f"| Field | Value |\n|---|---|\n"
        f"| Type | {wi_type} |\n"
        f"| State | {state} |\n"
        f"| Tags | {tags} |\n\n"
        f"## Description\n{desc}\n\n"
        f"## Acceptance Criteria\n{criteria}"
    )
    log_tool_call(_mcp_logger, "get_work_item", {"work_item_id": work_item_id}, result)
    return result


@mcp.tool()
def query_work_items(wiql: str, max_results: int = 10) -> str:
    """Run a WIQL query against Azure DevOps and return matching work items.

    Args:
        wiql: A WIQL query string (e.g. "SELECT [System.Id] FROM WorkItems WHERE ...").
        max_results: Maximum number of results to return (default 10, max 20).

    Returns:
        List of matching work items with ID, title, and state.
    """
    if not ORG or not PROJECT:
        return "Error: AZURE_DEVOPS_ORG and AZURE_DEVOPS_PROJECT must be set."

    max_results = min(max_results, 20)

    try:
        results = _run_az_json([
            "boards", "query",
            "--org", ORG,
            "--project", PROJECT,
            "--wiql", wiql,
        ])
    except (RuntimeError, json.JSONDecodeError) as e:
        return f"Error running WIQL query: {e}"

    if not results:
        return "No matching work items found."

    # Fetch details for each item (up to max_results).
    items = []
    for item in results[:max_results]:
        wi_id = item.get("id") or item.get("fields", {}).get("System.Id")
        if not wi_id:
            continue
        try:
            data = _run_az_json([
                "boards", "work-item", "show",
                "--id", str(wi_id),
                "--org", ORG,
            ])
            fields = data.get("fields", {})
            items.append(
                f"- **#{wi_id}** [{fields.get('System.State', '?')}] "
                f"{fields.get('System.Title', 'Unknown')}"
            )
        except Exception:
            items.append(f"- **#{wi_id}** (failed to fetch details)")

    result = f"Found {len(items)} work items:\n\n" + "\n".join(items)
    log_tool_call(_mcp_logger, "query_work_items", {"wiql": wiql[:100], "max_results": max_results}, result)
    return result


@mcp.tool()
def add_comment(work_item_id: int, comment: str) -> str:
    """Post a discussion comment to an Azure DevOps work item.

    Rate-limited to 5 comments per session to prevent spam.

    Args:
        work_item_id: The numeric work item ID.
        comment: The comment text (HTML supported).

    Returns:
        Confirmation or error message.
    """
    global _comments_posted

    if not ORG:
        return "Error: AZURE_DEVOPS_ORG not set."

    if _comments_posted >= _MAX_COMMENTS:
        return f"Error: Comment limit reached ({_MAX_COMMENTS} per session)."

    try:
        cmd = ["az", "boards", "work-item", "update",
               "--id", str(work_item_id),
               "--org", ORG,
               "--discussion", comment,
               "--output", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Error posting comment: {result.stderr.strip()[:500]}"
        _comments_posted += 1
        result = f"Comment posted to #{work_item_id} ({_comments_posted}/{_MAX_COMMENTS} this session)."
        log_tool_call(_mcp_logger, "add_comment", {"work_item_id": work_item_id, "comment_len": len(comment)}, result)
        return result
    except subprocess.TimeoutExpired:
        return "Error: az CLI timed out."
    except Exception as e:
        return f"Error posting comment: {e}"


def reset_limits() -> None:
    """Reset the comment counter (for testing)."""
    global _comments_posted
    _comments_posted = 0


if __name__ == "__main__":
    mcp.run()
