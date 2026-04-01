"""Tests for Phase 5: dai init (detection helpers) and dai doctor (health checks)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from src.setup import (
    _detect_framework,
    _detect_env_type,
    _detect_base_branch,
    _guess_module_path,
    _default_model,
    _check_python,
    _check_workspace,
    _check_module_path,
    _check_azure_config,
    _check_ai_provider,
    run_doctor,
)


# ── Framework detection ──


def test_detect_drupal(tmp_path):
    (tmp_path / "composer.json").write_text("{}")
    assert _detect_framework(tmp_path) == "drupal"


def test_detect_drupal_from_web_sites(tmp_path):
    (tmp_path / "web" / "sites").mkdir(parents=True)
    assert _detect_framework(tmp_path) == "drupal"


def test_detect_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]")
    assert _detect_framework(tmp_path) == "python"


def test_detect_python_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup")
    assert _detect_framework(tmp_path) == "python"


def test_detect_react(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18"}}')
    assert _detect_framework(tmp_path) == "react"


def test_detect_angular(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"@angular/core": "^17"}}')
    assert _detect_framework(tmp_path) == "angular"


def test_detect_java(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert _detect_framework(tmp_path) == "java"


def test_detect_dotnet(tmp_path):
    (tmp_path / "MyApp.csproj").write_text("<Project/>")
    assert _detect_framework(tmp_path) == "dotnet"


def test_detect_framework_fallback(tmp_path):
    # Empty directory → defaults to drupal
    assert _detect_framework(tmp_path) == "drupal"


# ── Env type detection ──


def test_detect_env_ddev(tmp_path):
    (tmp_path / ".ddev").mkdir()
    assert _detect_env_type(tmp_path) == "ddev"


def test_detect_env_lando(tmp_path):
    (tmp_path / ".lando.yml").write_text("recipe: drupal10")
    assert _detect_env_type(tmp_path) == "lando"


def test_detect_env_docker_compose(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("version: '3'")
    assert _detect_env_type(tmp_path) == "docker-compose"


def test_detect_env_native(tmp_path):
    assert _detect_env_type(tmp_path) == "native"


# ── Base branch detection ──


@patch("src.setup.subprocess.run")
def test_detect_base_branch_main(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="refs/remotes/origin/main\n")
    assert _detect_base_branch(Path("/tmp")) == "main"


@patch("src.setup.subprocess.run")
def test_detect_base_branch_fallback(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout="")
    assert _detect_base_branch(Path("/tmp")) == "master"


# ── Module path guessing ──


def test_guess_module_path():
    assert "modules" in _guess_module_path("drupal")
    assert _guess_module_path("python") == "src"
    assert _guess_module_path("react") == "src"
    assert "java" in _guess_module_path("java")
    assert _guess_module_path("dotnet") == "src"
    assert _guess_module_path("angular") == "src/app"


# ── Model defaults ──


def test_default_model():
    assert "claude" in _default_model("anthropic")
    assert "gpt" in _default_model("openai")
    assert "gpt" in _default_model("copilot")


# ── Doctor checks ──


def test_check_python():
    name, passed, _ = _check_python()
    assert passed  # We're running on >= 3.10


def test_check_workspace_ok(tmp_path):
    (tmp_path / ".git").mkdir()
    config = {"project": {"workspace_dir": str(tmp_path)}}
    _, passed, _ = _check_workspace(config)
    assert passed


def test_check_workspace_missing():
    config = {"project": {"workspace_dir": ""}}
    _, passed, _ = _check_workspace(config)
    assert not passed


def test_check_workspace_no_git(tmp_path):
    config = {"project": {"workspace_dir": str(tmp_path)}}
    _, passed, msg = _check_workspace(config)
    assert not passed
    assert "not a git repository" in msg


def test_check_module_path_ok(tmp_path):
    mod_dir = tmp_path / "src"
    mod_dir.mkdir()
    config = {"project": {"workspace_dir": str(tmp_path), "module_path": "src"}}
    _, passed, _ = _check_module_path(config)
    assert passed


def test_check_module_path_empty():
    config = {"project": {"workspace_dir": "/tmp", "module_path": ""}}
    _, passed, _ = _check_module_path(config)
    assert not passed


def test_check_module_path_missing(tmp_path):
    config = {"project": {"workspace_dir": str(tmp_path), "module_path": "nonexistent"}}
    _, passed, _ = _check_module_path(config)
    assert not passed


def test_check_azure_config_ok():
    config = {"azure_devops": {"organization": "https://dev.azure.com/org", "project": "proj"}}
    _, passed, _ = _check_azure_config(config)
    assert passed


def test_check_azure_config_missing():
    config = {"azure_devops": {"organization": "", "project": ""}}
    _, passed, _ = _check_azure_config(config)
    assert not passed


@patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}, clear=False)
def test_check_ai_provider_anthropic():
    config = {"ai_agent": {"provider": "anthropic"}}
    _, passed, _ = _check_ai_provider(config)
    assert passed


@patch.dict("os.environ", {}, clear=False)
def test_check_ai_provider_missing_key():
    # Ensure the key is not in env
    import os
    os.environ.pop("OPENAI_API_KEY", None)
    config = {"ai_agent": {"provider": "openai"}}
    _, passed, _ = _check_ai_provider(config)
    assert not passed


# ── run_doctor returns list ──


def test_run_doctor_returns_checks(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    config = {
        "project": {"workspace_dir": str(tmp_path), "module_path": "src", "base_branch": "master"},
        "azure_devops": {"organization": "https://dev.azure.com/o", "project": "p"},
        "ai_agent": {"provider": "copilot"},
        "local_env": {"type": "native"},
    }
    checks = run_doctor(config)
    assert len(checks) >= 8
    # Each check is (name, bool, msg)
    for name, passed, msg in checks:
        assert isinstance(name, str)
        assert isinstance(passed, bool)
        assert isinstance(msg, str)
