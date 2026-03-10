"""Tests for the config loader."""

from pathlib import Path
from unittest.mock import patch

from src.config import deep_merge, load_config


def test_deep_merge():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    override = {"b": {"c": 99}, "e": 5}
    result = deep_merge(base, override)
    assert result == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}


def test_deep_merge_empty():
    assert deep_merge({"a": 1}, {}) == {"a": 1}
    assert deep_merge({}, {"a": 1}) == {"a": 1}


def test_load_config_defaults():
    """Loading config from default location returns defaults."""
    config = load_config()
    assert "project" in config
    assert "azure_devops" in config
    assert "ai_agent" in config


def test_env_var_overrides():
    """Environment variables override config values."""
    env = {"ANTHROPIC_API_KEY": "test-key-123"}
    with patch.dict("os.environ", env):
        config = load_config()
        assert config["ai_agent"]["anthropic_api_key"] == "test-key-123"
