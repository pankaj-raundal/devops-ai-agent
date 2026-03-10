"""Pipeline orchestrator — ties all stages together into an end-to-end workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..agent.context_builder import build_story_context, save_story_context
from ..agent.implement import ImplementationAgent
from ..integrations.azure_devops import AzureDevOpsClient
from ..integrations.git_manager import GitManager
from ..integrations.zendesk import ZendeskClient
from ..reviewer.ai_reviewer import AIReviewer
from ..reviewer.test_runner import TestRunner

logger = logging.getLogger("devops_ai_agent.pipeline")


class Stage(str, Enum):
    FETCH_STORY = "fetch_story"
    CREATE_BRANCH = "create_branch"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    COMPLETE = "complete"


@dataclass
class PipelineResult:
    """Result of a pipeline run."""

    stage: Stage
    success: bool
    details: dict = field(default_factory=dict)
    error: str = ""


class Pipeline:
    """End-to-end pipeline: Fetch → Branch → Implement → Test → Review."""

    def __init__(self, config: dict):
        self.config = config
        self.devops = AzureDevOpsClient(config)
        self.git = GitManager(config)
        self.implementer = ImplementationAgent(config)
        self.test_runner = TestRunner(config)
        self.reviewer = AIReviewer(config)
        self.workspace = Path(config["project"]["workspace_dir"])

    def run(self, work_item_id: int | None = None, skip_tests: bool = False) -> list[PipelineResult]:
        """Execute the full pipeline. If work_item_id is None, fetch latest."""
        results: list[PipelineResult] = []

        # --- Stage 1: Fetch story ---
        logger.info("=== Stage 1: Fetch Story ===")
        if work_item_id:
            work_item = self.devops.get_work_item_details(work_item_id)
        else:
            work_item = self.devops.fetch_latest_story()

        if not work_item:
            r = PipelineResult(Stage.FETCH_STORY, False, error="No story found.")
            results.append(r)
            return results

        results.append(PipelineResult(Stage.FETCH_STORY, True, details={
            "id": work_item.id, "title": work_item.title
        }))
        logger.info("Story #%s: %s", work_item.id, work_item.title)

        # Build context.
        story_context = build_story_context(work_item)
        context_path = self.workspace / ".current-story.md"
        save_story_context(story_context, context_path)

        # --- Stage 2: Create branch ---
        logger.info("=== Stage 2: Create Branch ===")
        try:
            self.git.ensure_base_branch()
            branch_name = self.git.create_feature_branch(work_item.id, work_item.title)
            results.append(PipelineResult(Stage.CREATE_BRANCH, True, details={
                "branch": branch_name
            }))
            logger.info("Branch: %s", branch_name)
        except Exception as e:
            results.append(PipelineResult(Stage.CREATE_BRANCH, False, error=str(e)))
            return results

        # --- Stage 3: Implement ---
        logger.info("=== Stage 3: Implement ===")
        impl_result = self.implementer.implement(story_context)
        results.append(PipelineResult(Stage.IMPLEMENT, impl_result["success"], details=impl_result))

        if not impl_result["success"]:
            logger.error("Implementation failed: %s", impl_result.get("output", ""))
            return results
        logger.info("Implementation done via: %s", impl_result["method"])

        # Commit changes.
        changed = self.git.get_changed_files()
        if changed:
            self.git.commit_changes(f"feat(#{work_item.id}): {work_item.title[:50]}")
            logger.info("Committed %d changed files.", len(changed))

        # --- Stage 4: Test ---
        if not skip_tests:
            logger.info("=== Stage 4: Test ===")
            test_summary = self.test_runner.run_all()
            results.append(PipelineResult(Stage.TEST, test_summary.all_passed, details={
                "summary": test_summary.summary_text()
            }))
            if not test_summary.all_passed:
                logger.warning("Some tests failed:\n%s", test_summary.summary_text())
        else:
            logger.info("=== Stage 4: Test (skipped) ===")

        # --- Stage 5: Review ---
        logger.info("=== Stage 5: Review ===")
        diff = self.git.get_diff()
        review = self.reviewer.review(diff, story_context)
        results.append(PipelineResult(Stage.REVIEW, review["verdict"] == "APPROVE", details=review))
        logger.info("Review verdict: %s", review["verdict"])

        # --- Complete ---
        results.append(PipelineResult(Stage.COMPLETE, True, details={
            "branch": branch_name,
            "work_item_id": work_item.id,
        }))
        logger.info("=== Pipeline complete for #%s ===", work_item.id)

        return results

    def run_from_zendesk(self, ticket_id: int) -> list[PipelineResult]:
        """Full pipeline starting from Zendesk ticket: create DevOps story first."""
        zendesk = ZendeskClient(self.config)
        ticket = zendesk.get_full_ticket(ticket_id)
        if not ticket:
            return [PipelineResult(Stage.FETCH_STORY, False, error=f"Zendesk ticket #{ticket_id} not found.")]

        # Create DevOps work item from Zendesk ticket.
        description = (
            f"<h2>Zendesk Ticket #{ticket.id}</h2>\n"
            f"<p><b>Subject:</b> {ticket.subject}</p>\n"
            f"<p><b>Priority:</b> {ticket.priority}</p>\n"
            f"<p><b>Status:</b> {ticket.status}</p>\n"
            f"<p>{ticket.description}</p>\n"
        )
        if ticket.comments:
            description += "<h3>Customer Comments</h3>\n"
            for c in ticket.comments[:5]:
                description += f"<p>{c}</p>\n"

        work_item_id = self.devops.create_work_item(
            title=f"[Zendesk #{ticket.id}] {ticket.subject}",
            description=description,
            tags="auto,zendesk",
        )
        if not work_item_id:
            return [PipelineResult(Stage.FETCH_STORY, False, error="Failed to create DevOps work item.")]

        logger.info("Created DevOps work item #%s from Zendesk #%s", work_item_id, ticket_id)

        return self.run(work_item_id=work_item_id)
