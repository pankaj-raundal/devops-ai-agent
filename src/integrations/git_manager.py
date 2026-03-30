"""Git operations — branch management, commits, PRs."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.git")


class GitManager:
    """Manages git operations for the project workspace."""

    def __init__(self, config: dict):
        self.workspace_dir = Path(config["project"]["workspace_dir"])
        self.base_branch = config["project"].get("base_branch", "master")
        self.branch_prefix = config["project"].get("branch_prefix", "feature")
        git_config = config.get("git", {})
        self.auto_commit = git_config.get("auto_commit", True)
        self.auto_push = git_config.get("auto_push", False)
        self.auto_pr = git_config.get("auto_pr", False)
        self.pr_target_branch = git_config.get("pr_target_branch", "")  # Empty = use base_branch.
        self.commit_template = git_config.get(
            "commit_message_template",
            "#{work_item_id} - {title}",
        )

    def _run(self, *args: str, capture: bool = True) -> str:
        """Run a git command in the workspace directory."""
        cmd = ["git"] + list(args)
        logger.debug("git %s", " ".join(args))
        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=capture,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git {args[0]} timed out after 120s — check network or auth config.")
        if result.returncode != 0 and capture:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip() if capture else ""

    def current_branch(self) -> str:
        """Get the current branch name."""
        return self._run("branch", "--show-current")

    def reset_workspace(self) -> None:
        """Reset workspace to a clean state on the base branch.

        Stashes any uncommitted changes (recoverable via `git stash list`),
        then switches to the base branch and pulls latest. Call this before
        starting a new story to avoid conflicts from the previous story.
        """
        status = self._run("status", "--porcelain")
        if status:
            self._run("stash", "push", "-m", "auto-stash-before-story-switch")
            logger.warning("Stashed uncommitted changes before switching stories.")
        self.ensure_base_branch()

    def ensure_base_branch(self) -> None:
        """Switch to the base branch and pull latest.

        Automatically stashes uncommitted changes before switching/pulling
        to avoid 'cannot pull with rebase: unstaged changes' errors.
        """
        # Stash any uncommitted changes first.
        status = self._run("status", "--porcelain")
        if status:
            self._run("stash", "push", "-m", "auto-stash-before-base-branch")
            logger.warning("Stashed uncommitted changes before switching to base branch.")

        current = self.current_branch()
        if current != self.base_branch:
            try:
                self._run("checkout", self.base_branch)
            except RuntimeError:
                # Try common alternatives.
                alt = "main" if self.base_branch == "master" else "master"
                self._run("checkout", alt)
                self.base_branch = alt

        self._run("pull", "--rebase", "origin", self.base_branch)
        logger.info("On branch %s, up to date.", self.base_branch)

    def create_feature_branch(self, work_item_id: int, title: str) -> str:
        """Create and checkout a feature branch, reusing if it already exists."""
        # Sanitize title for branch name.
        slug = title.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")[:60]
        branch_name = f"{self.branch_prefix}/{work_item_id}-{slug}"

        # Check if branch already exists locally.
        existing = self._run("branch", "--list", branch_name)
        if existing.strip():
            logger.info("Branch %s already exists, switching to it.", branch_name)
            self._run("checkout", branch_name)
            return branch_name

        self.ensure_base_branch()
        self._run("checkout", "-b", branch_name)
        logger.info("Created branch: %s", branch_name)
        return branch_name

    def commit_changes(self, work_item_id: int, title: str, description: str = "") -> bool:
        """Stage and commit all changes."""
        if not self.auto_commit:
            logger.info("Auto-commit disabled, skipping.")
            return False

        # Check for changes.
        status = self._run("status", "--porcelain")
        if not status:
            logger.info("No changes to commit.")
            return False

        self._run("add", "-A")
        message = self.commit_template.format(
            work_item_id=work_item_id,
            title=title,
            description=description[:200] if description else "",
        )
        self._run("commit", "-m", message)
        logger.info("Committed: %s", message.split("\n")[0])
        return True

    def push_branch(self, branch_name: str = "") -> None:
        """Push a branch to origin."""
        if not branch_name:
            branch_name = self.current_branch()
        self._run("push", "-u", "origin", branch_name)
        logger.info("Pushed to origin/%s", branch_name)

    def has_feature_branch(self, work_item_id: int, title: str) -> str | None:
        """Check if a feature branch already exists for this work item."""
        slug = title.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")[:60]
        branch_name = f"{self.branch_prefix}/{work_item_id}-{slug}"
        existing = self._run("branch", "--list", branch_name)
        return branch_name if existing.strip() else None

    def get_diff(self, base: str | None = None) -> str:
        """Get the diff of changes from base branch."""
        if base is None:
            base = self.base_branch
        return self._run("diff", base, "--", ".")

    def get_changed_files(self, base: str | None = None) -> list[str]:
        """Get list of files changed since base branch."""
        if base is None:
            base = self.base_branch
        output = self._run("diff", "--name-only", base)
        return [f for f in output.splitlines() if f.strip()]

    def create_pull_request(
        self,
        work_item_id: int,
        title: str,
        description: str = "",
        branch_name: str = "",
    ) -> dict:
        """Create a Pull Request using GitHub CLI (``gh pr create``).

        Pushes the branch first if needed, then creates a PR targeting
        ``pr_target_branch`` (or ``base_branch`` if not configured).

        Returns dict with ``success``, ``url``, and ``error`` keys.
        """
        if not branch_name:
            branch_name = self.current_branch()

        target = self.pr_target_branch or self.base_branch

        # Build PR title and body.
        pr_title = f"#{work_item_id} - {title}"
        pr_body = (
            f"## Azure DevOps Work Item #{work_item_id}\n\n"
            f"{description[:2000] if description else 'Automated PR by DevOps AI Agent.'}\n\n"
            f"---\n"
            f"*Created automatically by [DevOps AI Agent](https://github.com/devops-ai-agent)*"
        )

        try:
            result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--base", target,
                    "--head", branch_name,
                    "--title", pr_title,
                    "--body", pr_body,
                ],
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                logger.info("PR created: %s", pr_url)
                return {"success": True, "url": pr_url, "error": ""}
            else:
                error = result.stderr.strip()
                # PR might already exist — not an error.
                if "already exists" in error.lower():
                    logger.info("PR already exists for branch %s.", branch_name)
                    return {"success": True, "url": "(already exists)", "error": ""}
                logger.error("gh pr create failed: %s", error)
                return {"success": False, "url": "", "error": error}
        except FileNotFoundError:
            logger.error("GitHub CLI (gh) not found. Install: https://cli.github.com/")
            return {"success": False, "url": "", "error": "gh CLI not installed"}
        except subprocess.TimeoutExpired:
            logger.error("gh pr create timed out.")
            return {"success": False, "url": "", "error": "Timed out"}
