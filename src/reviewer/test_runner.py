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
        self.framework = project.get("framework", "drupal")
        local_env = config.get("local_env", {})
        self.env_type = local_env.get("type", "native")
        self.container_cmd = local_env.get("container_command", local_env.get("type", "ddev"))

        # test_scope: "all" = full module, "changed" = only changed files
        git_config = config.get("git", {})
        self.test_scope = git_config.get("test_scope", "changed")

        # Load profile for extensions and checks.
        from src.profiles import get_profile
        self.profile = get_profile(config)

        # Load checks from config, or fall back to framework profile defaults.
        configured_checks = config.get("ai_agent", {}).get("checks")
        if configured_checks:
            self.checks = configured_checks
        else:
            self.checks = self.profile.get("checks", [])

        # Pull lintable extensions from profile (#10).
        self.file_extensions = set(self.profile.get("file_extensions", []))

    def run_all(self, changed_files: list[str] | None = None) -> TestSummary:
        """Run all configured checks and return summary.

        Args:
            changed_files: List of changed file paths (relative to workspace).
                Used when test_scope is 'changed' to limit lint/analysis scope.
        """
        summary = TestSummary()

        # Filter to relevant files for scoped checks.
        scoped_files = self._get_scoped_files(changed_files)

        for check in self.checks:
            handler = getattr(self, f"_run_{check}", None)
            if handler:
                logger.info("Running check: %s (scope: %s)", check, self.test_scope)
                result = handler(scoped_files)
                summary.results.append(result)
            else:
                logger.warning("Unknown check: %s — skipping", check)

        return summary

    def _needs_container(self) -> bool:
        """Whether commands should be run inside a container (#11)."""
        return self.env_type in ("ddev", "lando", "docker-compose")

    def _cmd(self, *args: str) -> list[str]:
        """Build a command, prepending container exec if needed (#11)."""
        if self._needs_container():
            return [self.container_cmd, "exec"] + list(args)
        return list(args)

    def _get_scoped_files(self, changed_files: list[str] | None) -> list[str]:
        """Return file paths to check based on test_scope config.

        Uses profile-driven extensions (#10) instead of hardcoded PHP list.
        """
        if self.test_scope != "changed" or not changed_files:
            return []

        scoped = []
        for f in changed_files:
            if self.module_path and not f.startswith(self.module_path):
                continue
            if Path(f).suffix in self.file_extensions:
                scoped.append(f)

        if scoped:
            logger.info("Test scope 'changed': %d file(s) targeted", len(scoped))
        else:
            logger.info("Test scope 'changed': no lintable files in changeset")
        return scoped

    # ── Drupal / PHP handlers ──

    def _run_phpunit(self, scoped_files: list[str]) -> TestResult:
        """Run PHPUnit tests (always runs full test suite)."""
        test_dir = f"{self.module_path}/tests/"
        cmd = self._cmd("phpunit", "-c", "web/core", test_dir)
        return self._exec("phpunit", cmd)

    def _run_phpcs(self, scoped_files: list[str]) -> TestResult:
        """Run PHP CodeSniffer — scoped to changed files when configured."""
        base_cmd = self._cmd(
            "phpcs", "--standard=Drupal,DrupalPractice",
            "--extensions=php,module,inc,install,test,profile,theme",
        )
        if scoped_files:
            cmd = base_cmd + scoped_files
        else:
            cmd = base_cmd + [self.module_path]
        return self._exec("phpcs", cmd)

    def _run_phpstan(self, scoped_files: list[str]) -> TestResult:
        """Run PHPStan static analysis — scoped to changed files when configured."""
        base_cmd = self._cmd("phpstan", "analyse")
        if scoped_files:
            cmd = base_cmd + scoped_files + ["--level=2", "--no-progress"]
        else:
            cmd = base_cmd + [self.module_path, "--level=2", "--no-progress"]
        return self._exec("phpstan", cmd)

    def _run_drush_cr(self, scoped_files: list[str]) -> TestResult:
        """Run drush cache rebuild to verify no fatal errors."""
        if self._needs_container():
            cmd = [self.container_cmd, "drush", "cr"]
        else:
            cmd = ["drush", "cr"]
        return self._exec("drush_cr", cmd)

    # ── Python handlers ──

    def _run_pytest(self, scoped_files: list[str]) -> TestResult:
        """Run pytest (always full suite — test discovery is project-wide)."""
        cmd = self._cmd("pytest", "-v", "--tb=short")
        return self._exec("pytest", cmd)

    def _run_ruff(self, scoped_files: list[str]) -> TestResult:
        """Run ruff linter — scoped to changed files when configured."""
        base_cmd = self._cmd("ruff", "check")
        if scoped_files:
            cmd = base_cmd + scoped_files
        else:
            cmd = base_cmd + [self.module_path or "."]
        return self._exec("ruff", cmd)

    def _run_mypy(self, scoped_files: list[str]) -> TestResult:
        """Run mypy type checker — scoped to changed files when configured."""
        base_cmd = self._cmd("mypy")
        if scoped_files:
            cmd = base_cmd + scoped_files
        else:
            cmd = base_cmd + [self.module_path or "."]
        return self._exec("mypy", cmd)

    # ── JavaScript / TypeScript handlers ──

    def _run_jest(self, scoped_files: list[str]) -> TestResult:
        """Run Jest test runner."""
        cmd = self._cmd("npx", "jest", "--ci", "--verbose")
        return self._exec("jest", cmd)

    def _run_eslint(self, scoped_files: list[str]) -> TestResult:
        """Run ESLint — scoped to changed files when configured."""
        base_cmd = self._cmd("npx", "eslint")
        if scoped_files:
            cmd = base_cmd + scoped_files
        else:
            cmd = base_cmd + [self.module_path or "."]
        return self._exec("eslint", cmd)

    def _run_tsc(self, scoped_files: list[str]) -> TestResult:
        """Run TypeScript compiler in check-only mode."""
        cmd = self._cmd("npx", "tsc", "--noEmit")
        return self._exec("tsc", cmd)

    # ── Angular handlers ──

    def _run_ng_test(self, scoped_files: list[str]) -> TestResult:
        """Run Angular test runner (Karma/Jest via ng test)."""
        cmd = self._cmd("npx", "ng", "test", "--watch=false", "--browsers=ChromeHeadless")
        return self._exec("ng_test", cmd)

    def _run_ng_lint(self, scoped_files: list[str]) -> TestResult:
        """Run Angular linter (ng lint)."""
        cmd = self._cmd("npx", "ng", "lint")
        return self._exec("ng_lint", cmd)

    # ── Java / Maven handlers ──

    def _run_mvn_test(self, scoped_files: list[str]) -> TestResult:
        """Run Maven tests."""
        cmd = self._cmd("mvn", "test", "-q")
        return self._exec("mvn_test", cmd)

    def _run_checkstyle(self, scoped_files: list[str]) -> TestResult:
        """Run Checkstyle via Maven."""
        cmd = self._cmd("mvn", "checkstyle:check", "-q")
        return self._exec("checkstyle", cmd)

    def _run_spotbugs(self, scoped_files: list[str]) -> TestResult:
        """Run SpotBugs via Maven."""
        cmd = self._cmd("mvn", "spotbugs:check", "-q")
        return self._exec("spotbugs", cmd)

    # ── .NET handlers ──

    def _run_dotnet_test(self, scoped_files: list[str]) -> TestResult:
        """Run dotnet test."""
        cmd = self._cmd("dotnet", "test", "--verbosity", "minimal")
        return self._exec("dotnet_test", cmd)

    def _run_dotnet_format(self, scoped_files: list[str]) -> TestResult:
        """Run dotnet format check (no fix, verify only)."""
        cmd = self._cmd("dotnet", "format", "--verify-no-changes")
        return self._exec("dotnet_format", cmd)

    def _run_dotnet_build(self, scoped_files: list[str]) -> TestResult:
        """Run dotnet build with warnings as errors."""
        cmd = self._cmd("dotnet", "build", "/warnaserror")
        return self._exec("dotnet_build", cmd)

    def auto_fix_lint(self) -> list[str]:
        """Run deterministic auto-fixers for lint tools. Returns list of tools that applied fixes."""
        target = self.module_path or "."
        fixers: dict[str, list[str]] = {
            "phpcs": self._cmd(
                "phpcbf", "--standard=Drupal,DrupalPractice",
                "--extensions=php,module,inc,install,test,profile,theme", target,
            ),
            "ruff": self._cmd("ruff", "check", "--fix", target),
            "eslint": self._cmd("npx", "eslint", "--fix", target),
            "dotnet_format": self._cmd("dotnet", "format", target),
            "checkstyle": self._cmd("mvn", "checkstyle:check", "-q"),
        }
        fixed = []
        for tool, cmd in fixers.items():
            if tool.replace("_", "") not in [c.replace("_", "") for c in self.checks]:
                continue
            try:
                result = subprocess.run(
                    cmd, cwd=self.workspace_dir,
                    capture_output=True, text=True, timeout=120,
                )
                # phpcbf returns 1 when it fixes files (not an error).
                if result.returncode in (0, 1) and result.stdout:
                    logger.info("Auto-fix %s: %s", tool, result.stdout[:200])
                    fixed.append(tool)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                logger.debug("Auto-fixer %s not available, skipping.", tool)
        return fixed

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
