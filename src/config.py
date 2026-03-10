"""Configuration loader — merges config.yaml with config.local.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_dir: str | Path | None = None) -> dict[str, Any]:
    """Load and merge configuration files.

    Priority: config.local.yaml > config.yaml > environment variables.
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"
    else:
        config_dir = Path(config_dir)

    # Load base config.
    base_path = config_dir / "config.yaml"
    if not base_path.exists():
        raise FileNotFoundError(f"Base config not found: {base_path}")
    with open(base_path) as f:
        config = yaml.safe_load(f) or {}

    # Merge local overrides.
    local_path = config_dir / "config.local.yaml"
    if local_path.exists():
        with open(local_path) as f:
            local_config = yaml.safe_load(f) or {}
        config = deep_merge(config, local_config)

    # Apply environment variable overrides.
    _apply_env_overrides(config)

    return config


def _apply_env_overrides(config: dict) -> None:
    """Override config values from environment variables."""
    env_map = {
        "ZENDESK_API_TOKEN": ("zendesk", "api_token"),
        "WEBHOOK_SECRET": ("webhook", "secret"),
        "ANTHROPIC_API_KEY": ("ai_agent", "anthropic_api_key"),
        "OPENAI_API_KEY": ("ai_agent", "openai_api_key"),
        "AZURE_DEVOPS_PAT": ("azure_devops", "pat"),
        "SLACK_WEBHOOK_URL": ("notifications", "slack_webhook"),
    }
    for env_var, path in env_map.items():
        value = os.environ.get(env_var)
        if value:
            obj = config
            for key in path[:-1]:
                obj = obj.setdefault(key, {})
            obj[path[-1]] = value
