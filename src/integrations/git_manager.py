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
        self.commit_template = git_config.get(
            "commit_message_template",
            "#{work_item_id} - {title}",
        )

    def _run(self, *args: str, capture: bool = True) -> str:
        """Run a git command in the workspace directory."""
        cmd = ["git"] + list(args)
        logger.debug("git %s", " ".join(args))
        result = subprocess.run(
            cmd,
            cwd=self.workspace_dir,
            capture_output=capture,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 and capture:
            raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip() if capture else ""

    def current_branch(self) -> str:
        """Get the current branch name."""
        return self._run("branch", "--show-current")

    def ensure_base_branch(self) -> None:
        """Switch to the base branch and pull latest."""
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

        if self.auto_push:
            branch = self.current_branch()
            self._run("push", "-u", "origin", branch)
            logger.info("Pushed to origin/%s", branch)

        return True

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
