"""Context builder — assembles the full story context for the AI agent."""

from __future__ import annotations

import logging
from pathlib import Path

from src.integrations.azure_devops import WorkItem

logger = logging.getLogger("devops_ai_agent.context")

CONTEXT_FILENAME = ".current-story.md"


def build_story_context(work_item: WorkItem, config: dict) -> str:
    """Build a markdown context document from a work item."""
    comments_md = ""
    if work_item.comments:
        for c in work_item.comments:
            comments_md += f"### {c['date']}\n{c['text']}\n\n"
    else:
        comments_md = "No comments yet.\n"

    module_path = config["project"].get("module_path", "")
    project_name = config["project"].get("name", "")
    local_env = config.get("local_env", {})
    drush = local_env.get("drush_prefix", "drush")

    context = f"""# Current Work Item

- **ID:** {work_item.id}
- **Type:** {work_item.work_item_type}
- **Title:** {work_item.title}
- **State:** {work_item.state}
- **Tags:** {work_item.tags}
- **URL:** {work_item.url}

## Description

{work_item.description}

## Acceptance Criteria

{work_item.acceptance_criteria}

## Discussion / Comments

{comments_md}

## Development Notes

- Project: {project_name}
- Module path: {module_path}
- Use `{drush}` for Drush commands
- Use `{drush} cr` to clear cache after changes
- Follow Drupal coding standards (PSR-12 with Drupal conventions)
- Ensure PHP 8.4 compatibility
"""
    return context


def save_story_context(work_item: WorkItem, config: dict) -> Path:
    """Save the story context to the workspace."""
    workspace = Path(config["project"]["workspace_dir"])
    context_file = workspace / CONTEXT_FILENAME
    content = build_story_context(work_item, config)
    context_file.write_text(content)
    logger.info("Story context saved to %s", context_file)
    return context_file


def load_story_context(config: dict) -> str | None:
    """Load existing story context from workspace."""
    workspace = Path(config["project"]["workspace_dir"])
    context_file = workspace / CONTEXT_FILENAME
    if context_file.exists():
        return context_file.read_text()
    return None
