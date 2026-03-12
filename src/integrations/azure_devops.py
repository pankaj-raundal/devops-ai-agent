"""Azure DevOps integration — query work items, fetch details, update state."""

from __future__ import annotations

import html
import logging
import re
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("devops_ai_agent.azure_devops")


@dataclass
class WorkItem:
    """Represents an Azure DevOps work item."""

    id: int
    title: str
    work_item_type: str = ""
    state: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    tags: str = ""
    comments: list[dict[str, str]] = field(default_factory=list)
    url: str = ""


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = html.unescape(clean)
    return clean.strip()


def _run_az(args: list[str]) -> str:
    """Run an az CLI command and return stdout."""
    cmd = ["az"] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"az command failed (exit {result.returncode}): {stderr}")
    return result.stdout.strip()


def _run_az_json(args: list[str]) -> dict | list:
    """Run an az CLI command and parse JSON output."""
    import json

    raw = _run_az(args + ["--output", "json"])
    if not raw:
        return []
    return json.loads(raw)


class AzureDevOpsClient:
    """Client for Azure DevOps operations via az CLI."""

    def __init__(self, config: dict):
        ado = config.get("azure_devops", {})
        self.org = ado.get("organization", "")
        self.project = ado.get("project", "")
        self.team = ado.get("team", "")
        self.assigned_to = ado.get("assigned_to", "")
        self.auto_tag = ado.get("auto_tag", "auto")
        self.states = ado.get("states", ["New", "Active"])
        self.current_sprint_only = ado.get("current_sprint_only", True)

    def fetch_latest_story(self) -> WorkItem | None:
        """Fetch the latest work item matching filters."""
        stories = self.fetch_all_stories()
        return stories[0] if stories else None

    def fetch_all_stories(self) -> list[WorkItem]:
        """Fetch all work items matching filters (ordered by CreatedDate DESC)."""
        wiql = self._build_wiql()
        logger.info("Querying Azure DevOps for stories tagged '%s'...", self.auto_tag)
        logger.debug("WIQL: %s", wiql)

        try:
            results = _run_az_json([
                "boards", "query",
                "--org", self.org,
                "--project", self.project,
                "--wiql", wiql,
            ])
        except RuntimeError as e:
            logger.error("Failed to query Azure DevOps: %s", e)
            return []

        if not results:
            logger.info("No matching stories found.")
            return []

        stories = []
        for item in results:
            work_item_id = item.get("id") or item.get("fields", {}).get("System.Id")
            if work_item_id:
                try:
                    stories.append(self.get_work_item_details(work_item_id))
                except Exception as e:
                    logger.warning("Failed to fetch details for #%s: %s", work_item_id, e)

        logger.info("Found %d matching story/stories.", len(stories))
        return stories

    def get_work_item_details(self, work_item_id: int) -> WorkItem:
        """Fetch full details for a work item."""
        logger.info("Fetching details for work item #%s...", work_item_id)
        data = _run_az_json([
            "boards", "work-item", "show",
            "--id", str(work_item_id),
            "--org", self.org,
        ])

        fields = data.get("fields", {})
        wi = WorkItem(
            id=work_item_id,
            title=fields.get("System.Title", ""),
            work_item_type=fields.get("System.WorkItemType", ""),
            state=fields.get("System.State", ""),
            description=_strip_html(fields.get("System.Description") or "No description provided"),
            acceptance_criteria=_strip_html(
                fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or "None specified"
            ),
            tags=fields.get("System.Tags", ""),
            url=f"{self.org}/{self.project}/_workitems/edit/{work_item_id}",
        )

        # Fetch discussion comments.
        wi.comments = self._fetch_comments(work_item_id)
        logger.info("Fetched %d comment(s) for #%s", len(wi.comments), work_item_id)
        return wi

    def create_work_item(
        self,
        title: str,
        description: str,
        work_item_type: str = "User Story",
        tags: str = "auto; zendesk",
        assigned_to: str = "",
    ) -> int:
        """Create a new work item in Azure DevOps."""
        if not assigned_to:
            assigned_to = self.assigned_to

        logger.info("Creating work item: %s", title)
        result = _run_az_json([
            "boards", "work-item", "create",
            "--org", self.org,
            "--project", self.project,
            "--type", work_item_type,
            "--title", title,
            "--description", description,
            "--assigned-to", assigned_to,
            "--fields", f"System.Tags={tags}",
        ])
        work_item_id = result.get("id")
        logger.info("Created work item #%s", work_item_id)
        return work_item_id

    def update_work_item_state(self, work_item_id: int, state: str) -> None:
        """Update the state of a work item."""
        logger.info("Updating work item #%s state to '%s'", work_item_id, state)
        _run_az([
            "boards", "work-item", "update",
            "--id", str(work_item_id),
            "--org", self.org,
            "--state", state,
        ])

    def add_comment(self, work_item_id: int, comment_html: str) -> bool:
        """Add a discussion comment to a work item via the REST API."""
        import json as _json
        import os

        logger.info("Adding comment to work item #%s...", work_item_id)

        # The `az boards work-item update` with --discussion field adds history comments.
        try:
            _run_az([
                "boards", "work-item", "update",
                "--id", str(work_item_id),
                "--org", self.org,
                "--discussion", comment_html,
            ])
            logger.info("Comment added to #%s.", work_item_id)
            return True
        except RuntimeError as e:
            logger.error("Failed to add comment to #%s: %s", work_item_id, e)
            return False

    def _fetch_comments(self, work_item_id: int) -> list[dict[str, str]]:
        """Fetch discussion comments from work item history."""
        try:
            data = _run_az_json([
                "devops", "invoke",
                "--org", self.org,
                "--area", "wit",
                "--resource", "updates",
                "--route-parameters", f"id={work_item_id}",
                "--api-version", "7.1",
                "--http-method", "GET",
            ])
        except RuntimeError:
            logger.warning("Could not fetch comments for #%s", work_item_id)
            return []

        comments = []
        for update in data.get("value", []):
            history_field = update.get("fields", {}).get("System.History", {})
            new_value = history_field.get("newValue", "")
            if new_value:
                comments.append({
                    "date": update.get("revisedDate", ""),
                    "text": _strip_html(new_value),
                })
        return comments

    def _build_wiql(self) -> str:
        """Build the WIQL query string."""
        states = ", ".join(f"'{s}'" for s in self.states)
        conditions = [
            f"[System.AssignedTo] CONTAINS '{self.assigned_to}'",
            f"[System.State] IN ({states})",
            f"[System.Tags] CONTAINS '{self.auto_tag}'",
        ]
        if self.current_sprint_only and self.team:
            iteration_path = f"[{self.project}]\\{self.team}"
            conditions.append(
                f"[System.IterationPath] = @CurrentIteration('{iteration_path}')"
            )

        where = " AND ".join(conditions)
        return (
            "SELECT [System.Id], [System.Title], [System.WorkItemType], [System.State] "
            f"FROM WorkItems WHERE {where} ORDER BY [System.CreatedDate] DESC"
        )
