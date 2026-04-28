"""Pipeline orchestrator — ties all stages together into an end-to-end workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .agent.analyzer import StoryAnalyzer
from .agent.context_builder import build_story_context, save_story_context
from .history import build_history_context, load_runs_for_story, save_run_record
from .agent.implement import ImplementationAgent
from .agent.plan import ImplementationPlan, apply_plan
from .integrations.azure_devops import AzureDevOpsClient
from .integrations.git_manager import GitManager
from .integrations.zendesk import ZendeskClient
from .reviewer.ai_reviewer import AIReviewer
from .reviewer.test_runner import TestRunner
from .utils.events import PipelineEvent, event_bus
from .utils.ticket_logger import TicketLogger

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

    def __init__(self, config: dict, ci_mode: bool = False):
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

        # Phase 1 config: trust levels, fix loop, story quality.
        ai_cfg = config.get("ai_agent", {})
        self.trust_level = ai_cfg.get("trust_level", "cautious")
        self.max_fix_attempts = ai_cfg.get("max_fix_attempts", 3)
        self.min_story_quality = ai_cfg.get("min_story_quality", 4)

        # CI mode: suppress all interactive prompts and auto-approve everything.
        # Activated by --ci flag or when running in a non-TTY environment (e.g. GitHub Actions).
        import sys
        self.ci_mode = ci_mode or not sys.stdin.isatty()
        if self.ci_mode:
            logger.info("CI mode active — all interactive gates suppressed, auto-approving.")
            self.trust_level = "full-auto"
            self.implementer.require_consent = False
            self.reviewer.require_consent = False if hasattr(self.reviewer, "require_consent") else None

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

    def run(self, work_item_id: int | None = None, skip_tests: bool = False, skip_analysis: bool = False, dry_run: bool = False, fresh: bool = False, skip_git_add: bool = False) -> list[PipelineResult]:
        """Execute the full pipeline. If work_item_id is None, fetch latest.

        If fresh=True, deletes any existing feature branch and starts clean.
        If skip_analysis=True, skips the AI analysis stage to save API quota.
        If skip_git_add=True, AI writes files but does not commit/push/PR.
        """
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

        # Thread story_id to all agents for token usage tracking.
        self.analyzer.story_id = work_item.id
        self.implementer.story_id = work_item.id
        self.reviewer.story_id = work_item.id

        # Create per-ticket log file in the devops-ai-agent directory (not the project workspace).
        tlog = TicketLogger(work_item.id)
        tlog.section("Story Fetched")
        tlog.kv("ID", str(work_item.id))
        tlog.kv("Title", work_item.title)
        tlog.kv("Type", work_item.work_item_type)
        tlog.kv("State", work_item.state)
        tlog.kv("Tags", work_item.tags)
        tlog.kv("Description", work_item.description[:1000])
        tlog.kv("Acceptance Criteria", work_item.acceptance_criteria[:1000])
        tlog.kv("Comments", str(len(work_item.comments)))
        # Thread ticket logger to implementation agent so it can log AI I/O.
        self.implementer.ticket_logger = tlog

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
        tlog.section("Story Context Sent to AI")
        tlog.write(story_context)
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

        # --- Generate MCP config for Claude CLI sessions ---
        # Creates .mcp.json with correct MODULE_PATH, workspace, and ADO env vars
        # so all Claude CLI calls (analyze, implement, review) get MCP tool access.
        try:
            from src.mcp.config import generate_mcp_config
            module_abs = str(self.workspace / self.implementer.module_path) if self.implementer.module_path else str(self.workspace)
            generate_mcp_config(module_abs, str(self.workspace), self.config)
            logger.info("MCP config generated for Claude CLI sessions.")
        except Exception as e:
            logger.warning("Failed to generate MCP config (Claude CLI will run without MCP): %s", e)

        # --- Stage 2: AI Analysis (non-blocking) ---
        # Analysis is helpful but not required — if it fails (e.g. rate limit),
        # skip it and proceed to implementation.
        analysis = None
        analysis_failed = False
        if skip_analysis:
            logger.info("=== Stage 2: AI Analysis (skipped via --skip-analysis) ===")
            event_bus.emit(PipelineEvent("analyze", "skipped", "Skipped via --skip-analysis"))
            results.append(PipelineResult(Stage.ANALYZE, True, details={"skipped": True}))
        else:
            event_bus.emit(PipelineEvent("analyze", "running", f"AI analyzing story via {self.analyzer.provider}/{self.analyzer.model}..."))
            logger.info("=== Stage 2: AI Analysis ===")
            analysis = self.analyzer.analyze(story_context)
            analysis_md = analysis.to_markdown()
            analysis_failed = analysis.summary.startswith("Analysis failed:")

            results.append(PipelineResult(Stage.ANALYZE, not analysis_failed, details={
                "requires_code_change": analysis.requires_code_change,
                "confidence": analysis.confidence,
                "complexity": analysis.estimated_complexity,
                "summary": analysis.summary[:300],
            }))

            if analysis_failed:
                logger.warning("Analysis failed — skipping quality gate and proceeding to implementation.")
                event_bus.emit(PipelineEvent("analyze", "warning",
                    f"Analysis skipped: {analysis.summary[:100]} — proceeding to implementation"))
            elif not analysis.summary:
                event_bus.emit(PipelineEvent("analyze", "fail", "AI analysis returned empty result"))
                return results
            else:
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
                # Check story quality before expensive implementation.
                quality_score = self._assess_story_quality(work_item, analysis)
                if quality_score < self.min_story_quality and analysis.requires_code_change:
                    logger.warning("Story quality too low (%d/%d). Posting feedback to ADO.", quality_score, 10)
                    quality_msg = self._build_quality_feedback(work_item, analysis, quality_score)
                    self._write_back_comment(work_item.id, zendesk_ticket_id, quality_msg)
                    event_bus.emit(PipelineEvent("analyze", "fail",
                        f"Story quality too low ({quality_score}/10) — posted feedback to ADO", {
                            "quality_score": quality_score,
                        }))
                    self._emit_alert("warning", f"Story #{work_item.id} skipped: quality {quality_score}/10")
                    self._transition_state(work_item.id, self.state_on_no_code)
                    results.append(PipelineResult(Stage.ANALYZE, False, error=f"Story quality {quality_score}/10 (min: {self.min_story_quality})"))
                    return results

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
                    self._append_mcp_logs(tlog)
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

            # Auto-detect failed previous run and start fresh.
            if existing_branch and not fresh:
                prev_runs = load_runs_for_story(work_item.id)
                if prev_runs:
                    last_run = prev_runs[-1]
                    if last_run.get("success") == 0:  # SQLite stores bool as 0/1
                        if self.trust_level == "full-auto":
                            logger.info("Previous run failed. Trust=full-auto: auto-discarding old branch.")
                            fresh = True
                        else:
                            logger.warning("Previous run for #%s failed. Use --fresh to discard and retry clean.", work_item.id)

            if existing_branch and fresh:
                logger.info("--fresh: deleting old branch %s and starting clean.", existing_branch)
                self.git.ensure_base_branch()
                self.git._run("branch", "-D", existing_branch)
                existing_branch = None

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
        # Build full_context early so it's available for fix loop regardless of resume path.
        history_ctx = build_history_context(self.config, work_item.id)
        full_context = story_context
        if history_ctx:
            full_context = story_context + "\n\n" + history_ctx
            logger.info("Including history from %d previous run(s).", history_ctx.count("### Run"))

        # Feed analysis insights into the CLI prompt so vague PM stories
        # still produce targeted implementations (zero extra API cost).
        if not skip_analysis and analysis and not analysis_failed:
            parts = []
            if analysis.summary:
                parts.append(analysis.summary)
            if analysis.approach:
                parts.append(f"Approach: {analysis.approach}")
            if analysis.affected_areas:
                parts.append(f"Affected files: {', '.join(analysis.affected_areas)}")
            if parts:
                self.implementer.analysis_hint = "\n".join(parts)
                logger.info("Analysis hint set (%d chars) for CLI prompt.", len(self.implementer.analysis_hint))

        # Capture lint baseline BEFORE implementation. This lets the fix loop
        # distinguish pre-existing errors from ones the AI introduced.
        lint_baseline = self.test_runner.run_all(changed_files=[])
        logger.info("Lint baseline captured: %s",
                     "all passing" if lint_baseline.all_passed
                     else ", ".join(r.tool for r in lint_baseline.results if not r.passed))

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

                # Get approval — auto-approve in autonomous/full-auto trust, else prompt.
                if self.trust_level in ("autonomous", "full-auto"):
                    logger.info("Trust level '%s': auto-approving plan.", self.trust_level)
                    for fc in plan.file_changes:
                        fc.approved = True
                    plan.approved = True
                else:
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
        tlog.section("Implementation Result")
        tlog.kv("Method", impl_result.get("method", ""))
        tlog.kv("Success", str(impl_result.get("success", False)))
        tlog.write(impl_result.get("output", "")[:5000])

        # Commit changes (skipped if --skip-git-add).
        if skip_git_add:
            changed = self.git.get_changed_files()
            logger.info("--skip-git-add: %d file(s) written but NOT staged/committed. Review with 'git diff'.", len(changed) if changed else 0)
            event_bus.emit(PipelineEvent("commit", "skipped",
                f"Skipped — {len(changed) if changed else 0} file(s) written, not committed (--skip-git-add)", {
                    "files": changed[:20] if changed else [],
                }))
            tlog.section("Git Add/Commit Skipped (--skip-git-add)")
            tlog.kv("Changed files", str(changed) if changed else "None")
            results.append(PipelineResult(Stage.COMPLETE, True, details={
                "branch": branch_name,
                "work_item_id": work_item.id,
                "skip_git_add": True,
                "files_written": changed or [],
            }))
            event_bus.emit(PipelineEvent("complete", "pass",
                f"Files written — review with 'git diff' then commit manually", {
                    "branch": branch_name,
                    "skip_git_add": True,
                }))
            tlog.section("Pipeline Complete (skip-git-add)")
            tlog.kv("Branch", branch_name)
            self._append_mcp_logs(tlog)
            tlog.close()
            save_run_record(self.config, {
                "work_item_id": work_item.id,
                "failed_stage": None,
                "method": impl_result.get("method", ""),
                "ai_output": impl_result.get("output", "")[:300],
                "branch": branch_name,
            })
            return results

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

        # --- Stage 5: Test (with iterative fix loop) ---
        if not skip_tests:
            event_bus.emit(PipelineEvent("test", "running", "Running tests..."))
            logger.info("=== Stage 5: Test ===")

            raw_summary = self.test_runner.run_all(changed_files=changed if changed else None)
            # Filter out pre-existing lint errors so the fix loop doesn't chase them.
            test_summary = raw_summary.new_errors_only(lint_baseline)

            # --- Iterative fix loop: retry up to max_fix_attempts ---
            attempt = 1
            while not test_summary.all_passed and attempt < self.max_fix_attempts:
                attempt += 1
                logger.info("Test failures detected. Auto-fix attempt %d/%d...", attempt, self.max_fix_attempts)
                event_bus.emit(PipelineEvent("test", "running",
                    f"Fix attempt {attempt}/{self.max_fix_attempts} — feeding errors back to AI..."))

                # Step 1: Try auto-fixing lint errors first (deterministic, no AI cost).
                auto_fixed = self.test_runner.auto_fix_lint(changed_files=changed)
                if auto_fixed:
                    logger.info("Auto-lint-fix applied changes for: %s", ", ".join(auto_fixed))
                    changed = self.git.get_changed_files()
                    if changed:
                        self.git.commit_changes(work_item.id, f"Auto-fix lint (attempt {attempt})")
                    # Re-run tests after auto-fix.
                    raw_summary = self.test_runner.run_all(changed_files=changed if changed else None)
                    test_summary = raw_summary.new_errors_only(lint_baseline)
                    if test_summary.all_passed:
                        logger.info("Tests pass after auto-lint-fix.")
                        break

                # Step 2: Feed test errors back to AI for a fix attempt.
                # Use a slim context to stay within token limits (e.g. GitHub Models 8K).
                # The AI doesn't need the full story to fix lint — just the errors,
                # the changed file paths, and a brief reminder of the story.
                # Include module_path so tool-use can resolve file reads.
                changed_list = "\n".join(f"- {f}" for f in (changed or []))
                fix_context = (
                    f"## Story\n\n"
                    f"**#{work_item.id}** — {work_item.title}\n\n"
                    f"## Development Notes\n\n"
                    f"- Module path: {self.implementer.module_path}\n"
                    f"- Workspace: {self.workspace}\n\n"
                    f"## Changed Files\n\n{changed_list}\n\n"
                    f"## Test Failures (Attempt {attempt}/{self.max_fix_attempts})\n\n"
                    f"{test_summary.summary_text()}\n\n"
                    f"Fix ONLY the errors listed above. These are NEW errors introduced "
                    f"by your changes — pre-existing errors have already been filtered out. "
                    f"Do NOT try to fix other parts of the codebase."
                )
                fix_result = self.implementer.implement(fix_context)
                if fix_result.get("success"):
                    # Apply fix — handle plan-review or direct mode.
                    fix_plan = fix_result.get("plan")
                    if fix_plan:
                        # In autonomous/full-auto trust, auto-approve fix plans.
                        if self.trust_level in ("autonomous", "full-auto"):
                            for fc in fix_plan.file_changes:
                                fc.approved = True
                            fix_plan.approved = True
                        else:
                            fix_plan = self._get_plan_approval(fix_plan)
                        if fix_plan.approved:
                            apply_plan(fix_plan, self.workspace, self.implementer.module_path)
                    changed = self.git.get_changed_files()
                    if changed:
                        self.git.commit_changes(work_item.id, f"Fix attempt {attempt}")
                    raw_summary = self.test_runner.run_all(changed_files=changed if changed else None)
                    test_summary = raw_summary.new_errors_only(lint_baseline)
                else:
                    logger.warning("Fix attempt %d: AI implementation failed.", attempt)
                    break

            results.append(PipelineResult(Stage.TEST, test_summary.all_passed, details={
                "summary": test_summary.summary_text(),
                "fix_attempts": attempt,
            }))
            status = "pass" if test_summary.all_passed else "fail"
            event_bus.emit(PipelineEvent("test", status, test_summary.summary_text()[:200], {
                "summary": test_summary.summary_text(),
                "fix_attempts": attempt,
            }))
            if not test_summary.all_passed:
                logger.warning("Tests still failing after %d attempt(s):\n%s", attempt, test_summary.summary_text())
                self._emit_alert("warning", f"Tests failed for #{work_item.id} after {attempt} attempt(s)")
                save_run_record(self.config, {
                    "work_item_id": work_item.id,
                    "failed_stage": "test",
                    "method": impl_result.get("method", ""),
                    "error": test_summary.summary_text()[:500],
                    "fix_attempts": attempt,
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
        # APPROVE and COMMENT are both passing verdicts; only REQUEST_CHANGES / ERROR fail.
        review_passed = review["verdict"] in ("APPROVE", "COMMENT")
        results.append(PipelineResult(Stage.REVIEW, review_passed, details=review))
        review_status = "pass" if review_passed else "fail"
        event_bus.emit(PipelineEvent("review", review_status,
                                     f"Verdict: {review['verdict']}", {
                                         "verdict": review["verdict"],
                                         "summary": review.get("summary", "")[:500],
                                     }))
        logger.info("Review verdict: %s", review["verdict"])
        tlog.section("Code Review")
        tlog.kv("Verdict", review.get("verdict", ""))
        tlog.write(review.get("summary", "")[:3000])

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

        if self.trust_level == "full-auto":
            logger.info("Trust level 'full-auto': auto-approving push.")
            push_approved = True
        else:
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
        tlog.section("Pipeline Complete")
        tlog.kv("Branch", branch_name)
        tlog.kv("PR URL", pr_url or "N/A")

        # --- Append MCP tool-call logs ---
        self._append_mcp_logs(tlog)

        tlog.close()

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

    def _append_mcp_logs(self, tlog) -> None:
        """Collect MCP tool-call logs from this pipeline run and append to ticket log."""
        from pathlib import Path
        log_dir = Path(__file__).parent.parent / ".dai" / "logs"
        if not log_dir.exists():
            return

        mcp_logs = sorted(log_dir.glob("mcp-*.log"))
        if not mcp_logs:
            return

        tlog.section("MCP Tool Call Logs")
        for log_file in mcp_logs:
            try:
                content = log_file.read_text(encoding="utf-8").strip()
                if content:
                    # Filename format: mcp-<server-name>-YYYYMMDD_HHMMSS.log
                    # Strip "mcp-" prefix and "-YYYYMMDD_HHMMSS" suffix.
                    stem = log_file.stem  # e.g. "mcp-azure-devops-20260428_090534"
                    parts = stem.split("-")
                    server_name = "-".join(parts[1:-1]) if len(parts) >= 3 else stem
                    tlog.kv(f"Server: {server_name}", "")
                    tlog.write(content)
                    logger.info("MCP log: %s (%d lines)", log_file.name, content.count("\n") + 1)
            except Exception as e:
                logger.warning("Failed to read MCP log %s: %s", log_file.name, e)

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

    def _assess_story_quality(self, work_item, analysis) -> int:
        """Score story quality on a 1-10 scale based on available information."""
        score = 0
        desc = work_item.description or ""

        # Has a description at all (2 points).
        if len(desc.strip()) > 20:
            score += 2
        elif desc.strip():
            score += 1

        # Description length — more detail = higher quality (2 points).
        if len(desc) > 200:
            score += 2
        elif len(desc) > 50:
            score += 1

        # Has acceptance criteria (2 points).
        ac_keywords = ["acceptance criteria", "expected", "given", "when", "then", "should"]
        desc_lower = desc.lower()
        if any(kw in desc_lower for kw in ac_keywords):
            score += 2

        # AI confidence is high (1 point).
        if analysis.confidence == "high":
            score += 1

        # AI identified specific affected areas (1 point).
        if analysis.affected_areas and len(analysis.affected_areas) >= 1:
            score += 1

        # AI has no open questions (1 point).
        if not analysis.questions:
            score += 1

        # Complexity is not unknown/unclear (1 point).
        if analysis.estimated_complexity in ("trivial", "simple", "moderate", "complex"):
            score += 1

        return min(score, 10)

    @staticmethod
    def _build_quality_feedback(work_item, analysis, score: int) -> str:
        """Build a markdown comment coaching the PM to improve story quality."""
        issues = []
        desc = work_item.description or ""

        if len(desc.strip()) < 20:
            issues.append("Description is too short or missing")
        ac_keywords = ["acceptance criteria", "expected", "given", "when", "then", "should"]
        if not any(kw in desc.lower() for kw in ac_keywords):
            issues.append("No acceptance criteria found")
        if analysis.confidence == "low":
            issues.append("AI confidence is low — story may be ambiguous")
        if analysis.questions:
            issues.append(f"AI has {len(analysis.questions)} clarifying question(s)")

        issues_md = "\n".join(f"- {i}" for i in issues) if issues else "- General quality below threshold"
        questions_md = "\n".join(f"- {q}" for q in analysis.questions) if analysis.questions else ""

        return (
            f"## 📋 Story Quality Assessment\n\n"
            f"**Score:** {score}/10 (minimum required: 4)\n\n"
            f"This story needs more detail before AI implementation can proceed.\n\n"
            f"### Issues Found\n{issues_md}\n\n"
            + (f"### AI Questions\n{questions_md}\n\n" if questions_md else "")
            + f"### Suggestions\n"
            f"- Add acceptance criteria: *\"When X happens, Y should change to Z\"*\n"
            f"- Describe the expected behavior in detail\n"
            f"- Mention specific files or modules if known\n\n"
            f"---\n*Generated by DevOps AI Agent*"
        )

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
