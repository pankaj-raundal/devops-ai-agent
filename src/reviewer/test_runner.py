"""Test runner — executes project-specific tests and lint checks."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.test_runner")


@dataclass
class TestResult:
    """Result from running a test suite or lint check."""

    tool: str
    passed: bool
    output: str
    returncode: int


@dataclass
class TestSummary:
    """Aggregated summary of all tests and checks."""

    results: list[TestResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def summary_text(self) -> str:
        lines = []
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(f"[{status}] {r.tool}")
            if not r.passed:
                # Include first 40 lines of output for failures.
                truncated = "\n".join(r.output.splitlines()[:40])
                lines.append(truncated)
        return "\n".join(lines)


class TestRunner:
    """Runs configured test suites for a project."""

    def __init__(self, config: dict):
        self.config = config
        project = config.get("project", {})
        self.workspace_dir = Path(project.get("workspace_dir", "."))
        self.module_path = project.get("module_path", "")
        local_env = config.get("local_env", {})
        self.container_cmd = local_env.get("container_command", "ddev")
        self.checks = config.get("ai_agent", {}).get("checks", [
            "phpunit", "phpcs", "phpstan", "drush_cr"
        ])

    def run_all(self) -> TestSummary:
        """Run all configured checks and return summary."""
        summary = TestSummary()

        for check in self.checks:
            handler = getattr(self, f"_run_{check}", None)
            if handler:
                logger.info("Running check: %s", check)
                result = handler()
                summary.results.append(result)
            else:
                logger.warning("Unknown check: %s — skipping", check)

        return summary

    def _run_phpunit(self) -> TestResult:
        """Run PHPUnit tests."""
        test_dir = f"{self.module_path}/tests/"
        cmd = [
            self.container_cmd, "exec",
            "phpunit", "-c", "web/core", test_dir,
        ]
        return self._exec("phpunit", cmd)

    def _run_phpcs(self) -> TestResult:
        """Run PHP CodeSniffer."""
        cmd = [
            self.container_cmd, "exec",
            "phpcs", "--standard=Drupal,DrupalPractice",
            "--extensions=php,module,inc,install,test,profile,theme",
            self.module_path,
        ]
        return self._exec("phpcs", cmd)

    def _run_phpstan(self) -> TestResult:
        """Run PHPStan static analysis."""
        cmd = [
            self.container_cmd, "exec",
            "phpstan", "analyse", self.module_path,
            "--level=2", "--no-progress",
        ]
        return self._exec("phpstan", cmd)

    def _run_drush_cr(self) -> TestResult:
        """Run drush cache rebuild to verify no fatal errors."""
        cmd = [self.container_cmd, "drush", "cr"]
        return self._exec("drush_cr", cmd)

    def _exec(self, tool: str, cmd: list[str]) -> TestResult:
        """Execute a command and return a TestResult."""
        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return TestResult(
                tool=tool,
                passed=result.returncode == 0,
                output=result.stdout + result.stderr,
                returncode=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                tool=tool, passed=False, output="Timed out after 300s", returncode=-1
            )
        except FileNotFoundError:
            return TestResult(
                tool=tool, passed=False, output=f"Command not found: {cmd[0]}", returncode=-1
            )
