"""Pipeline orchestrator — ties all stages together into an end-to-end workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .agent.analyzer import StoryAnalyzer
from .agent.context_builder import build_story_context, build_history_context, save_story_context, save_run_record
from .agent.implement import ImplementationAgent
from .agent.plan import ImplementationPlan, apply_plan
from .integrations.azure_devops import AzureDevOpsClient
from .integrations.git_manager import GitManager
from .integrations.zendesk import ZendeskClient
from .reviewer.ai_reviewer import AIReviewer
from .reviewer.test_runner import TestRunner
from .utils.events import PipelineEvent, event_bus

logger = logging.getLogger("devops_ai_agent.pipeline")


class Stage(str, Enum):
    FETCH_STORY = "fetch_story"
    ANALYZE = "analyze"
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
    """End-to-end pipeline: Fetch → Analyze → Branch → Implement → Test → Review → Write-back."""

    def __init__(self, config: dict):
        self.config = config
        self.devops = AzureDevOpsClient(config)
        self.git = GitManager(config)
        self.analyzer = StoryAnalyzer(config)
        self.implementer = ImplementationAgent(config)
        self.test_runner = TestRunner(config)
        self.reviewer = AIReviewer(config)
        self.workspace = Path(config["project"]["workspace_dir"])

        # State transitions (configurable via azure_devops section).
        az_cfg = config.get("azure_devops", {})
        self.state_on_success = az_cfg.get("state_on_success", "Testing")
        self.state_on_no_code = az_cfg.get("state_on_no_code", "Evaluation")
        self.state_on_failure = az_cfg.get("state_on_failure", "")
        self._queue_mode = False  # Set True by run_queue() to preserve event history.
        self._plan_approval_callback: callable | None = None  # Set by dashboard for async approval.
        self._push_approval_callback: callable | None = None  # Set by dashboard for push confirmation.

    def set_plan_approval_callback(self, callback: callable) -> None:
        """Set a callback for plan approval (used by dashboard).

        The callback receives an ImplementationPlan and should return it with
        file_changes[].approved set for each accepted file. It should also set
        plan.approved = True if the overall plan is accepted.
        """
        self._plan_approval_callback = callback

    def set_push_approval_callback(self, callback: callable) -> None:
        """Set a callback for push confirmation (used by dashboard).

        The callback receives branch_name and returns True if user approves push.
        """
        self._push_approval_callback = callback

    def run(self, work_item_id: int | None = None, skip_tests: bool = False, dry_run: bool = False) -> list[PipelineResult]:
        """Execute the full pipeline. If work_item_id is None, fetch latest."""
        results: list[PipelineResult] = []
        if not self._queue_mode:
            event_bus.clear_history()
        zendesk_ticket_id = None  # Populated if story originated from Zendesk.

        # --- Stage 1: Fetch story ---
        event_bus.emit(PipelineEvent("fetch_story", "running", "Fetching story from Azure DevOps..."))
        logger.info("=== Stage 1: Fetch Story ===")
        if work_item_id:
            work_item = self.devops.get_work_item_details(work_item_id)
        else:
            work_item = self.devops.fetch_latest_story()

        if not work_item:
            r = PipelineResult(Stage.FETCH_STORY, False, error="No story found.")
            results.append(r)
            event_bus.emit(PipelineEvent("fetch_story", "fail", "No story found"))
            return results

        results.append(PipelineResult(Stage.FETCH_STORY, True, details={
            "id": work_item.id, "title": work_item.title
        }))
        event_bus.emit(PipelineEvent("fetch_story", "pass", f"#{work_item.id} {work_item.title}", {
            "id": work_item.id,
            "title": work_item.title,
            "type": work_item.work_item_type,
            "state": work_item.state,
            "tags": work_item.tags,
            "description": work_item.description[:500],
            "acceptance_criteria": work_item.acceptance_criteria[:500],
            "comments_count": len(work_item.comments),
        }))
        logger.info("Story #%s: %s", work_item.id, work_item.title)

        # Extract Zendesk ticket ID from title if present (e.g. "[Zendesk #123]").
        import re
        zd_match = re.search(r"\[Zendesk #(\d+)\]", work_item.title)
        if zd_match:
            zendesk_ticket_id = int(zd_match.group(1))

        # Build context.
        event_bus.emit(PipelineEvent("build_context", "running", "Building story context..."))
        story_context = build_story_context(work_item, self.config)
        save_story_context(work_item, self.config)
        event_bus.emit(PipelineEvent("build_context", "pass", f"Context built ({len(story_context):,} chars)", {
            "context_length": len(story_context),
            "preview": story_context[:300],
        }))

        if dry_run:
            logger.info("=== Dry Run — stopping before analysis/branch/implement/test/review ===")
            event_bus.emit(PipelineEvent("complete", "pass", "Dry run complete"))
            results.append(PipelineResult(Stage.COMPLETE, True, details={
                "dry_run": True,
                "work_item_id": work_item.id,
                "story_context_preview": story_context[:500],
            }))
            return results

        # --- Stage 2: AI Analysis ---
        event_bus.emit(PipelineEvent("analyze", "running", f"AI analyzing story via {self.analyzer.provider}/{self.analyzer.model}..."))
        logger.info("=== Stage 2: AI Analysis ===")
        analysis = self.analyzer.analyze(story_context)
        analysis_md = analysis.to_markdown()

        results.append(PipelineResult(Stage.ANALYZE, bool(analysis.summary), details={
            "requires_code_change": analysis.requires_code_change,
            "confidence": analysis.confidence,
            "complexity": analysis.estimated_complexity,
            "summary": analysis.summary[:300],
        }))

        if not analysis.summary:
            event_bus.emit(PipelineEvent("analyze", "fail", "AI analysis returned empty result"))
            return results

        event_bus.emit(PipelineEvent("analyze", "pass",
                                     f"Code change: {'Yes' if analysis.requires_code_change else 'No'} | "
                                     f"Confidence: {analysis.confidence} | Complexity: {analysis.estimated_complexity}", {
                                         "requires_code_change": analysis.requires_code_change,
                                         "confidence": analysis.confidence,
                                         "complexity": analysis.estimated_complexity,
                                         "summary": analysis.summary[:500],
                                         "approach": analysis.approach[:500],
                                         "risks": analysis.risks,
                                         "questions": analysis.questions,
                                         "affected_areas": analysis.affected_areas,
                                         "recommendation": analysis.recommendation[:500],
                                     }))
        logger.info("Analysis: code_change=%s, confidence=%s, complexity=%s",
                     analysis.requires_code_change, analysis.confidence, analysis.estimated_complexity)

        # Write analysis back to Azure DevOps as a comment.
        event_bus.emit(PipelineEvent("write_back", "running", "Writing analysis to Azure DevOps..."))
        self._write_back_comment(work_item.id, zendesk_ticket_id, analysis_md)
        event_bus.emit(PipelineEvent("write_back", "pass", "Analysis posted to work item"))

        # --- Smart Flow Decision ---
        if not analysis.requires_code_change:
            # No code change needed — summarize findings and complete.
            logger.info("AI determined no code change is required. Writing summary and completing.")
            event_bus.emit(PipelineEvent("complete", "pass",
                                         f"No code change needed — analysis posted to #{work_item.id}", {
                                             "work_item_id": work_item.id,
                                             "requires_code_change": False,
                                             "recommendation": analysis.recommendation,
                                         }))
            event_bus.emit(PipelineEvent("alert", "pass",
                                         f"Story #{work_item.id}: No code change required. Recommendation posted to ticket.",
                                         {"type": "info"}))
            # Transition state → Evaluation (no code change).
            self._transition_state(work_item.id, self.state_on_no_code)

            results.append(PipelineResult(Stage.COMPLETE, True, details={
                "work_item_id": work_item.id,
                "requires_code_change": False,
            }))
            save_run_record(self.config, {
                "work_item_id": work_item.id,
                "failed_stage": None,
                "requires_code_change": False,
                "analysis_summary": analysis.summary[:300],
            })
            return results

        # --- Stage 3: Create branch (code change path) ---
        event_bus.emit(PipelineEvent("create_branch", "running", "Creating feature branch..."))
        logger.info("=== Stage 3: Create Branch ===")
        try:
            existing_branch = self.git.has_feature_branch(work_item.id, work_item.title)
            if existing_branch:
                logger.info("Resuming on existing branch: %s", existing_branch)
                event_bus.emit(PipelineEvent("create_branch", "running", f"Reusing existing branch: {existing_branch}"))

            branch_name = self.git.create_feature_branch(work_item.id, work_item.title)
            resumed = existing_branch is not None
            results.append(PipelineResult(Stage.CREATE_BRANCH, True, details={
                "branch": branch_name, "resumed": resumed,
            }))
            label = f"{branch_name} (resumed)" if resumed else branch_name
            event_bus.emit(PipelineEvent("create_branch", "pass", label, {"branch": branch_name, "resumed": resumed}))
            logger.info("Branch: %s", branch_name)
        except Exception as e:
            results.append(PipelineResult(Stage.CREATE_BRANCH, False, error=str(e)))
            event_bus.emit(PipelineEvent("create_branch", "fail", str(e)))
            self._emit_alert("error", f"Failed to create branch for #{work_item.id}: {e}")
            self._transition_state(work_item.id, self.state_on_failure)
            return results

        # --- Stage 4: Implement ---
        # Skip implementation if there are already changes on this branch (resume scenario).
        existing_changes = self.git.get_changed_files()
        if existing_changes:
            logger.info("Existing changes detected (%d files) - skipping AI implementation.", len(existing_changes))
            event_bus.emit(PipelineEvent("implement", "pass", f"Skipped - {len(existing_changes)} file(s) already changed (resumed)", {
                "method": "resumed",
                "files": existing_changes[:20],
            }))
            results.append(PipelineResult(Stage.IMPLEMENT, True, details={
                "method": "resumed", "files_changed": len(existing_changes),
            }))
            impl_result = {"success": True, "method": "resumed", "output": "Resumed from previous run"}
        else:
            event_bus.emit(PipelineEvent("implement", "running", f"AI implementing via {self.implementer.provider}..."))
            logger.info("=== Stage 4: Implement ===")
            history_ctx = build_history_context(self.config, work_item.id)
            full_context = story_context
            if history_ctx:
                full_context = story_context + "\n\n" + history_ctx
                logger.info("Including history from %d previous run(s).", history_ctx.count("### Run"))
            impl_result = self.implementer.implement(full_context)

            # --- Plan-review flow: if a plan is returned, get approval before applying ---
            plan: ImplementationPlan | None = impl_result.get("plan")
            if plan and impl_result["success"]:
                logger.info("Plan generated with %d file change(s). Awaiting approval...", len(plan.file_changes))
                event_bus.emit(PipelineEvent("implement", "plan_ready",
                    f"Plan ready: {len(plan.file_changes)} file(s) — awaiting approval", {
                        "method": impl_result["method"],
                        "plan": plan.to_dict(),
                    }))

                # Get approval — via dashboard callback or CLI prompt.
                plan = self._get_plan_approval(plan)

                if not plan.approved:
                    logger.info("Plan rejected by user.")
                    event_bus.emit(PipelineEvent("implement", "fail", "Plan rejected by user"))
                    results.append(PipelineResult(Stage.IMPLEMENT, False, error="Plan rejected"))
                    self._emit_alert("warning", f"Plan rejected for #{work_item.id}")
                    return results

                # Apply approved file changes.
                event_bus.emit(PipelineEvent("implement", "running", "Applying approved changes..."))
                approved_count = sum(1 for fc in plan.file_changes if fc.approved)
                logger.info("Applying %d approved file change(s)...", approved_count)

                apply_result = apply_plan(plan, self.workspace, self.implementer.module_path)
                logger.info("Applied %d file(s), skipped %d.",
                            apply_result["total_applied"], apply_result["total_skipped"])

                if apply_result["total_applied"] == 0:
                    event_bus.emit(PipelineEvent("implement", "fail", "No file changes were approved/applied"))
                    results.append(PipelineResult(Stage.IMPLEMENT, False, error="No changes applied"))
                    return results

                impl_result["output"] = (
                    f"Plan applied: {apply_result['total_applied']} file(s) written, "
                    f"{apply_result['total_skipped']} skipped.\n\n{plan.to_markdown()}"
                )
                event_bus.emit(PipelineEvent("implement", "pass",
                    f"Plan applied: {apply_result['total_applied']} file(s)", {
                        "method": impl_result["method"],
                        "applied": apply_result["applied"],
                        "skipped": apply_result["skipped"],
                    }))

        results.append(PipelineResult(Stage.IMPLEMENT, impl_result["success"], details=impl_result))

        if not impl_result["success"]:
            logger.error("Implementation failed: %s", impl_result.get("output", ""))
            event_bus.emit(PipelineEvent("implement", "fail", impl_result.get("output", "")[:200]))
            self._emit_alert("error", f"Implementation failed for #{work_item.id}")
            self._write_back_comment(
                work_item.id, zendesk_ticket_id,
                f"## ⚠ Implementation Failed\n\n{impl_result.get('output', '')[:500]}\n\n---\n*Generated by DevOps AI Agent*",
            )
            self._transition_state(work_item.id, self.state_on_failure)
            save_run_record(self.config, {
                "work_item_id": work_item.id,
                "failed_stage": "implement",
                "method": impl_result.get("method", ""),
                "error": impl_result.get("output", "")[:500],
            })
            return results
        event_bus.emit(PipelineEvent("implement", "pass", f"Done via {impl_result['method']}", {
            "method": impl_result["method"],
            "output_preview": impl_result.get("output", "")[:500],
        }))
        logger.info("Implementation done via: %s", impl_result["method"])

        # Commit changes.
        event_bus.emit(PipelineEvent("commit", "running", "Committing changes..."))
        changed = self.git.get_changed_files()
        if changed:
            self.git.commit_changes(work_item.id, work_item.title)
            logger.info("Committed %d changed files.", len(changed))
            event_bus.emit(PipelineEvent("commit", "pass", f"{len(changed)} file(s) committed", {
                "files": changed[:20],
            }))
        else:
            event_bus.emit(PipelineEvent("commit", "pass", "No files changed"))

        # --- Stage 5: Test ---
        if not skip_tests:
            event_bus.emit(PipelineEvent("test", "running", "Running tests..."))
            logger.info("=== Stage 5: Test ===")
            test_summary = self.test_runner.run_all(changed_files=changed if changed else None)
            results.append(PipelineResult(Stage.TEST, test_summary.all_passed, details={
                "summary": test_summary.summary_text()
            }))
            status = "pass" if test_summary.all_passed else "fail"
            event_bus.emit(PipelineEvent("test", status, test_summary.summary_text()[:200], {
                "summary": test_summary.summary_text(),
            }))
            if not test_summary.all_passed:
                logger.warning("Some tests failed:\n%s", test_summary.summary_text())
                self._emit_alert("warning", f"Tests failed for #{work_item.id}")
                save_run_record(self.config, {
                    "work_item_id": work_item.id,
                    "failed_stage": "test",
                    "method": impl_result.get("method", ""),
                    "error": test_summary.summary_text()[:500],
                    "ai_output": impl_result.get("output", "")[:300],
                })
        else:
            logger.info("=== Stage 5: Test (skipped) ===")
            event_bus.emit(PipelineEvent("test", "skipped", "Tests skipped"))

        # --- Stage 6: Review ---
        event_bus.emit(PipelineEvent("review", "running", "Running AI code review..."))
        logger.info("=== Stage 6: Review ===")
        diff = self.git.get_diff()
        review = self.reviewer.review(diff, story_context)
        results.append(PipelineResult(Stage.REVIEW, review["verdict"] == "APPROVE", details=review))
        review_status = "pass" if review["verdict"] == "APPROVE" else "fail"
        event_bus.emit(PipelineEvent("review", review_status,
                                     f"Verdict: {review['verdict']}", {
                                         "verdict": review["verdict"],
                                         "summary": review.get("summary", "")[:500],
                                     }))
        logger.info("Review verdict: %s", review["verdict"])

        # Write final results back to tickets.
        final_comment = self._build_completion_comment(
            work_item.id, branch_name, impl_result, review,
            changed or [], skip_tests,
        )
        self._write_back_comment(work_item.id, zendesk_ticket_id, final_comment)

        # Transition state → Testing (successful pipeline).
        self._transition_state(work_item.id, self.state_on_success)

        # --- Stage 7: Push confirmation ---
        pr_url = ""
        event_bus.emit(PipelineEvent("push", "running", f"Branch {branch_name} ready — awaiting push confirmation..."))
        logger.info("=== Stage 7: Push Confirmation ===")

        push_approved = self._get_push_approval(branch_name)

        if push_approved:
            try:
                self.git.push_branch(branch_name)
                event_bus.emit(PipelineEvent("push", "pass", f"Pushed {branch_name} to origin", {
                    "branch": branch_name,
                }))
                logger.info("Pushed branch %s to origin.", branch_name)
            except RuntimeError as e:
                event_bus.emit(PipelineEvent("push", "fail", f"Push failed: {e}"))
                logger.error("Failed to push branch: %s", e)

            # --- Stage 8: Pull Request (optional, only after push) ---
            if self.git.auto_pr:
                event_bus.emit(PipelineEvent("pr", "running", "Creating Pull Request..."))
                logger.info("=== Stage 8: Pull Request ===")
                pr_result = self.git.create_pull_request(
                    work_item_id=work_item.id,
                    title=work_item.title,
                    description=work_item.description or "",
                    branch_name=branch_name,
                )
                if pr_result["success"]:
                    pr_url = pr_result["url"]
                    event_bus.emit(PipelineEvent("pr", "pass", f"PR created: {pr_url}", {
                        "url": pr_url,
                    }))
                    logger.info("PR created: %s", pr_url)
                else:
                    event_bus.emit(PipelineEvent("pr", "fail", f"PR creation failed: {pr_result['error']}"))
                    logger.warning("PR creation failed: %s", pr_result["error"])
        else:
            event_bus.emit(PipelineEvent("push", "pass", f"Push declined — branch {branch_name} stays local", {
                "branch": branch_name,
                "pushed": False,
            }))
            logger.info("Push declined by user. Branch %s remains local.", branch_name)

        # --- Complete ---
        results.append(PipelineResult(Stage.COMPLETE, True, details={
            "branch": branch_name,
            "work_item_id": work_item.id,
            "pr_url": pr_url,
        }))
        event_bus.emit(PipelineEvent("complete", "pass", f"Pipeline complete for #{work_item.id}", {
            "branch": branch_name,
            "work_item_id": work_item.id,
            "pr_url": pr_url,
        }))
        self._emit_alert("success", f"Pipeline complete for #{work_item.id} — branch: {branch_name}")
        logger.info("=== Pipeline complete for #%s ===", work_item.id)

        save_run_record(self.config, {
            "work_item_id": work_item.id,
            "failed_stage": None,
            "method": impl_result.get("method", ""),
            "ai_output": impl_result.get("output", "")[:300],
            "review_verdict": review.get("verdict", ""),
            "branch": branch_name,
        })

        return results

    # --- Helper methods ---

    def _get_plan_approval(self, plan: ImplementationPlan) -> ImplementationPlan:
        """Get approval for an implementation plan.

        If a dashboard callback is set, use it (async approval via UI).
        Otherwise, fall back to CLI interactive approval.
        """
        if self._plan_approval_callback:
            return self._plan_approval_callback(plan)
        return self._cli_plan_approval(plan)

    def _cli_plan_approval(self, plan: ImplementationPlan) -> ImplementationPlan:
        """Interactive CLI approval of an implementation plan."""
        from rich.console import Console
        from rich.panel import Panel

        console = Console()
        console.print()
        console.rule("[bold yellow]📋 Implementation Plan — Review Required[/]")
        console.print()
        console.print(f"  [bold]Summary:[/]  {plan.summary}")
        console.print(f"  [bold]Approach:[/] {plan.approach}")
        console.print(f"  [bold]Files:[/]    {len(plan.file_changes)} change(s)")
        if plan.risks:
            console.print(f"  [bold red]Risks:[/]    {', '.join(plan.risks)}")
        console.print()

        for i, fc in enumerate(plan.file_changes, 1):
            console.print(Panel(
                f"[bold]{fc.action.upper()}[/] — {fc.description}\n"
                f"Content: {len(fc.content)} chars",
                title=f"[cyan]{i}. {fc.path}[/]",
                border_style="cyan",
            ))
            try:
                answer = console.input(
                    f"  Approve this change? [Y]es / [N]o / [V]iew content: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[red]Aborted.[/]")
                return plan

            if answer in ("v", "view"):
                console.print(Panel(
                    fc.content[:5000] + ("\n... (truncated)" if len(fc.content) > 5000 else ""),
                    title="File Content",
                    border_style="dim",
                ))
                try:
                    answer = console.input("  Approve after review? [Y]es / [N]o: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[red]Aborted.[/]")
                    return plan

            fc.approved = answer in ("y", "yes", "")
            status = "[green]✓ Approved[/]" if fc.approved else "[red]✗ Rejected[/]"
            console.print(f"  {status}")
            console.print()

        approved_count = sum(1 for fc in plan.file_changes if fc.approved)
        console.print(f"[bold]Approved {approved_count}/{len(plan.file_changes)} file changes.[/]")

        if approved_count > 0:
            try:
                final = console.input(
                    "Apply approved changes? [Y]es / [N]o: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[red]Aborted.[/]")
                return plan
            plan.approved = final in ("y", "yes", "")
        else:
            plan.approved = False

        return plan

    def _get_push_approval(self, branch_name: str) -> bool:
        """Get user confirmation before pushing branch to origin.

        If a dashboard callback is set, use it (async approval via UI).
        Otherwise, fall back to CLI interactive prompt.
        """
        if self._push_approval_callback:
            return self._push_approval_callback(branch_name)
        return self._cli_push_approval(branch_name)

    def _cli_push_approval(self, branch_name: str) -> bool:
        """Interactive CLI prompt for push confirmation."""
        from rich.console import Console

        console = Console()
        console.print()
        console.rule("[bold yellow]🚀 Push Confirmation[/]")
        console.print(f"\n  Branch [cyan]{branch_name}[/] is ready.")
        console.print("  Push to origin? The branch currently exists only locally.\n")

        try:
            answer = console.input("  Push to origin? [Y]es / [N]o: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]Aborted — branch stays local.[/]")
            return False

        return answer in ("y", "yes", "")

    def _write_back_comment(self, work_item_id: int, zendesk_ticket_id: int | None, comment_md: str) -> None:
        """Post a comment to Azure DevOps (and Zendesk if applicable)."""
        try:
            # Azure DevOps expects HTML — convert markdown to simple HTML.
            comment_html = comment_md.replace("\n", "<br>")
            self.devops.add_comment(work_item_id, comment_html)
            logger.info("Posted comment to Azure DevOps work item #%s", work_item_id)
        except Exception as e:
            logger.error("Failed to write comment to Azure DevOps #%s: %s", work_item_id, e)

        if zendesk_ticket_id:
            try:
                zendesk = ZendeskClient(self.config)
                zendesk.add_comment(zendesk_ticket_id, comment_md, public=False)
                logger.info("Posted comment to Zendesk ticket #%s", zendesk_ticket_id)
            except Exception as e:
                logger.error("Failed to write comment to Zendesk #%s: %s", zendesk_ticket_id, e)

    def _emit_alert(self, alert_type: str, message: str) -> None:
        """Emit a dashboard alert event (success/error/warning/info)."""
        event_bus.emit(PipelineEvent("alert", alert_type, message, {"type": alert_type}))

    def _transition_state(self, work_item_id: int, new_state: str) -> None:
        """Transition a work item to a new state. Skip if new_state is empty."""
        if not new_state:
            return
        try:
            self.devops.update_work_item_state(work_item_id, new_state)
            logger.info("Transitioned #%s → %s", work_item_id, new_state)
            event_bus.emit(PipelineEvent("state_change", "pass",
                                         f"#{work_item_id} → {new_state}",
                                         {"work_item_id": work_item_id, "new_state": new_state}))
        except Exception as e:
            logger.error("Failed to transition #%s to '%s': %s", work_item_id, new_state, e)

    @staticmethod
    def _build_completion_comment(
        work_item_id: int,
        branch_name: str,
        impl_result: dict,
        review: dict,
        changed_files: list[str],
        skip_tests: bool,
    ) -> str:
        """Build a markdown summary comment for the completed pipeline."""
        verdict = review.get("verdict", "N/A")
        verdict_icon = "✅" if verdict == "APPROVE" else "⚠"
        method = impl_result.get("method", "N/A")
        file_list = "\n".join(f"- `{f}`" for f in changed_files[:20]) if changed_files else "- No files changed"
        test_note = "Skipped" if skip_tests else "Executed"

        return (
            f"## {verdict_icon} Pipeline Complete — #{work_item_id}\n\n"
            f"| Attribute | Value |\n"
            f"|-----------|-------|\n"
            f"| Branch | `{branch_name}` |\n"
            f"| Implementation method | {method} |\n"
            f"| Files changed | {len(changed_files)} |\n"
            f"| Review verdict | {verdict} |\n"
            f"| Tests | {test_note} |\n\n"
            f"### Changed Files\n{file_list}\n\n"
            f"### Review Summary\n{review.get('summary', 'N/A')[:500]}\n\n"
            f"---\n*Generated by DevOps AI Agent*"
        )

    def run_queue(self, skip_tests: bool = False, dry_run: bool = False) -> dict[int, list[PipelineResult]]:
        """Fetch all matching stories and process them sequentially.

        Returns a dict mapping work_item_id → list of PipelineResult.
        """
        stories = self.devops.fetch_all_stories()

        if not stories:
            logger.info("No stories in queue.")
            event_bus.emit(PipelineEvent("queue", "pass", "No stories found in queue", {"total": 0}))
            return {}

        total = len(stories)
        queue_items = [
            {"id": s.id, "title": s.title, "state": s.state, "status": "queued"}
            for s in stories
        ]
        event_bus.emit(PipelineEvent("queue", "running",
                                     f"{total} story/stories in queue", {
                                         "total": total,
                                         "stories": queue_items,
                                     }))
        logger.info("Processing queue: %d stories", total)

        self._queue_mode = True
        event_bus.clear_history()  # Clear once at start of the full queue.
        all_results: dict[int, list[PipelineResult]] = {}

        for idx, story in enumerate(stories):
            position = idx + 1
            # Emit queue progress.
            queue_items[idx]["status"] = "in_progress"
            event_bus.emit(PipelineEvent("queue", "running",
                                         f"Processing {position}/{total}: #{story.id} {story.title}", {
                                             "total": total,
                                             "current": position,
                                             "current_id": story.id,
                                             "current_title": story.title,
                                             "stories": queue_items,
                                         }))

            logger.info("=== Queue [%d/%d]: Story #%s ===", position, total, story.id)
            # Reset workspace to clean master before each story to avoid
            # leftover changes from the previous story bleeding across.
            try:
                self.git.reset_workspace()
            except Exception as e:
                logger.error("Failed to reset workspace before story #%s: %s", story.id, e)
            results = self.run(work_item_id=story.id, skip_tests=skip_tests, dry_run=dry_run)
            all_results[story.id] = results

            # Update queue item status.
            success = all(r.success for r in results)
            queue_items[idx]["status"] = "done" if success else "failed"

            event_bus.emit(PipelineEvent("queue", "running",
                                         f"Completed {position}/{total}: #{story.id} — {'✓' if success else '✗'}", {
                                             "total": total,
                                             "completed": position,
                                             "current_id": story.id,
                                             "success": success,
                                             "stories": queue_items,
                                         }))

        # Final queue summary.
        done_count = sum(1 for items in queue_items if items["status"] == "done")
        failed_count = sum(1 for items in queue_items if items["status"] == "failed")
        event_bus.emit(PipelineEvent("queue", "pass",
                                     f"Queue complete: {done_count} passed, {failed_count} failed, {total} total", {
                                         "total": total,
                                         "done": done_count,
                                         "failed": failed_count,
                                         "stories": queue_items,
                                     }))
        self._emit_alert("success" if failed_count == 0 else "warning",
                         f"Queue done: {done_count}/{total} succeeded")
        logger.info("Queue complete: %d passed, %d failed.", done_count, failed_count)

        self._queue_mode = False
        return all_results

    def run_from_zendesk(self, ticket_id: int, dry_run: bool = False) -> list[PipelineResult]:
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

        return self.run(work_item_id=work_item_id, dry_run=dry_run)
