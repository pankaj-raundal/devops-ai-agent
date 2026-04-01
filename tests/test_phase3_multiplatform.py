"""Tests for Phase 3: multi-framework test runners, profile-driven extensions,
container-aware commands, and multi-platform PR creation."""

from unittest.mock import patch, MagicMock

from src.reviewer.test_runner import TestRunner, TestResult
from src.integrations.git_manager import GitManager


# ── Helper: build config for a given framework ──

def _config(framework: str = "python", env_type: str = "native", module_path: str = "src"):
    return {
        "project": {
            "workspace_dir": "/tmp/test-workspace",
            "module_path": module_path,
            "framework": framework,
            "base_branch": "main",
        },
        "local_env": {"type": env_type},
        "git": {"test_scope": "changed"},
        "ai_agent": {},
    }


# ── #10 Profile-driven scoped extensions ──


def test_python_extensions():
    runner = TestRunner(_config("python"))
    assert ".py" in runner.file_extensions
    assert ".pyi" in runner.file_extensions
    assert ".php" not in runner.file_extensions


def test_drupal_extensions():
    runner = TestRunner(_config("drupal"))
    assert ".php" in runner.file_extensions
    assert ".module" in runner.file_extensions
    assert ".py" not in runner.file_extensions


def test_react_extensions():
    runner = TestRunner(_config("react"))
    assert ".ts" in runner.file_extensions
    assert ".tsx" in runner.file_extensions
    assert ".jsx" in runner.file_extensions


def test_java_extensions():
    runner = TestRunner(_config("java"))
    assert ".java" in runner.file_extensions


def test_dotnet_extensions():
    runner = TestRunner(_config("dotnet"))
    assert ".cs" in runner.file_extensions
    assert ".csproj" in runner.file_extensions


def test_angular_extensions():
    runner = TestRunner(_config("angular"))
    assert ".ts" in runner.file_extensions
    assert ".html" in runner.file_extensions


def test_scoped_files_uses_profile_extensions():
    runner = TestRunner(_config("python", module_path="src"))
    result = runner._get_scoped_files(["src/app.py", "src/readme.txt", "other.py"])
    assert "src/app.py" in result
    assert "src/readme.txt" not in result  # .txt not in python profile
    assert "other.py" not in result  # outside module_path


# ── #11 Container-aware command building ──


def test_native_no_container_prefix():
    runner = TestRunner(_config("python", env_type="native"))
    assert not runner._needs_container()
    cmd = runner._cmd("pytest", "-v")
    assert cmd == ["pytest", "-v"]


def test_ddev_adds_container_prefix():
    runner = TestRunner(_config("drupal", env_type="ddev"))
    assert runner._needs_container()
    cmd = runner._cmd("phpunit", "-c", "web/core")
    assert cmd == ["ddev", "exec", "phpunit", "-c", "web/core"]


def test_docker_compose_needs_container():
    runner = TestRunner(_config("python", env_type="docker-compose"))
    assert runner._needs_container()


def test_lando_needs_container():
    runner = TestRunner(_config("drupal", env_type="lando"))
    assert runner._needs_container()


# ── #9 Test runner handlers: verify correct checks loaded per framework ──


def test_python_checks():
    runner = TestRunner(_config("python"))
    assert "pytest" in runner.checks
    assert "ruff" in runner.checks
    assert "mypy" in runner.checks


def test_drupal_checks():
    runner = TestRunner(_config("drupal"))
    assert "phpunit" in runner.checks
    assert "phpcs" in runner.checks
    assert "phpstan" in runner.checks


def test_react_checks():
    runner = TestRunner(_config("react"))
    assert "jest" in runner.checks
    assert "eslint" in runner.checks
    assert "tsc" in runner.checks


def test_java_checks():
    runner = TestRunner(_config("java"))
    assert "mvn_test" in runner.checks
    assert "checkstyle" in runner.checks
    assert "spotbugs" in runner.checks


def test_dotnet_checks():
    runner = TestRunner(_config("dotnet"))
    assert "dotnet_test" in runner.checks
    assert "dotnet_format" in runner.checks
    assert "dotnet_build" in runner.checks


def test_angular_checks():
    runner = TestRunner(_config("angular"))
    assert "ng_test" in runner.checks
    assert "ng_lint" in runner.checks
    assert "tsc" in runner.checks


def test_handler_resolved_for_all_python_checks():
    """Every check listed in the python profile has a matching _run_X handler."""
    runner = TestRunner(_config("python"))
    for check in runner.checks:
        handler = getattr(runner, f"_run_{check}", None)
        assert handler is not None, f"No handler _run_{check} for check '{check}'"


def test_handler_resolved_for_all_drupal_checks():
    runner = TestRunner(_config("drupal"))
    for check in runner.checks:
        handler = getattr(runner, f"_run_{check}", None)
        assert handler is not None, f"No handler _run_{check} for check '{check}'"


def test_handler_resolved_for_all_react_checks():
    runner = TestRunner(_config("react"))
    for check in runner.checks:
        handler = getattr(runner, f"_run_{check}", None)
        assert handler is not None, f"No handler _run_{check} for check '{check}'"


def test_handler_resolved_for_all_java_checks():
    runner = TestRunner(_config("java"))
    for check in runner.checks:
        handler = getattr(runner, f"_run_{check}", None)
        assert handler is not None, f"No handler _run_{check} for check '{check}'"


def test_handler_resolved_for_all_dotnet_checks():
    runner = TestRunner(_config("dotnet"))
    for check in runner.checks:
        handler = getattr(runner, f"_run_{check}", None)
        assert handler is not None, f"No handler _run_{check} for check '{check}'"


def test_handler_resolved_for_all_angular_checks():
    runner = TestRunner(_config("angular"))
    for check in runner.checks:
        handler = getattr(runner, f"_run_{check}", None)
        assert handler is not None, f"No handler _run_{check} for check '{check}'"


# ── #12 Multi-platform PR detection ──


def _git_config():
    return {
        "project": {
            "workspace_dir": "/tmp/test-workspace",
            "module_path": "src",
            "base_branch": "main",
        },
        "git": {},
    }


@patch("src.integrations.git_manager.subprocess.run")
def test_detect_platform_github(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout="https://github.com/user/repo.git", stderr=""
    )
    gm = GitManager(_git_config())
    assert gm._detect_platform() == "github"


@patch("src.integrations.git_manager.subprocess.run")
def test_detect_platform_azure_devops(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout="https://dev.azure.com/myorg/myproject/_git/myrepo", stderr=""
    )
    gm = GitManager(_git_config())
    assert gm._detect_platform() == "azure-devops"


@patch("src.integrations.git_manager.subprocess.run")
def test_detect_platform_azure_visualstudio(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout="https://myorg.visualstudio.com/myproject/_git/myrepo", stderr=""
    )
    gm = GitManager(_git_config())
    assert gm._detect_platform() == "azure-devops"


@patch("src.integrations.git_manager.subprocess.run")
def test_detect_platform_gitlab(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0, stdout="https://gitlab.com/user/repo.git", stderr=""
    )
    gm = GitManager(_git_config())
    assert gm._detect_platform() == "gitlab"


@patch("src.integrations.git_manager.subprocess.run")
def test_detect_platform_fallback(mock_run):
    mock_run.side_effect = RuntimeError("no remote")
    gm = GitManager(_git_config())
    assert gm._detect_platform() == "github"
