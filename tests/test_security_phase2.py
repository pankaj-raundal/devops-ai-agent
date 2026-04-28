"""Phase 2 security tests — preflight, sensitive write blocklist, write quota."""

from __future__ import annotations

import os
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
class TestPreflightAdoToken:
    def test_no_pat_returns_high(self, monkeypatch):
        from src.security.preflight import check_ado_token
        monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
        monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
        f = check_ado_token({})
        assert f.level == "HIGH"
        assert f.code == "ADO_NO_SCOPED_TOKEN"

    def test_pat_present_returns_info(self, monkeypatch):
        from src.security.preflight import check_ado_token
        monkeypatch.setenv("AZURE_DEVOPS_PAT", "fakepat")
        # No org configured → skip the probe.
        f = check_ado_token({})
        assert f.level == "INFO"
        assert f.code == "ADO_PAT_PRESENT"


class TestPreflightGitHubToken:
    def test_no_token_is_info(self, monkeypatch):
        from src.security.preflight import check_github_token
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        f = check_github_token()
        assert f.level == "INFO"
        assert f.code == "GH_NO_TOKEN"

    def test_overscoped_classic_pat_is_critical(self, monkeypatch):
        from src.security.preflight import check_github_token
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")

        class FakeResp:
            status_code = 200
            headers = {"x-oauth-scopes": "repo, admin:org, delete_repo"}

        with mock.patch("httpx.get", return_value=FakeResp()):
            f = check_github_token()
        assert f.level == "CRITICAL"
        assert f.code == "GH_OVERSCOPED"
        assert "admin:org" in f.detail["scopes"] or "delete_repo" in f.detail["scopes"]

    def test_fine_grained_pat_is_info(self, monkeypatch):
        from src.security.preflight import check_github_token
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_fake")

        class FakeResp:
            status_code = 200
            headers = {}  # fine-grained: no x-oauth-scopes

        with mock.patch("httpx.get", return_value=FakeResp()):
            f = check_github_token()
        assert f.level == "INFO"
        assert f.code == "GH_FINE_GRAINED"

    def test_invalid_token_is_critical(self, monkeypatch):
        from src.security.preflight import check_github_token
        monkeypatch.setenv("GITHUB_TOKEN", "bad")

        class FakeResp:
            status_code = 401
            headers = {}

        with mock.patch("httpx.get", return_value=FakeResp()):
            f = check_github_token()
        assert f.level == "CRITICAL"
        assert f.code == "GH_TOKEN_INVALID"


class TestPreflightCloudCreds:
    def test_aws_secret_flagged(self, monkeypatch):
        from src.security.preflight import check_cloud_creds
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
        findings = check_cloud_creds()
        assert any("AWS_SECRET_ACCESS_KEY" in f.detail.get("vars", []) for f in findings)
        assert all(f.level == "HIGH" for f in findings)

    def test_clean_env_no_findings(self, monkeypatch):
        from src.security.preflight import check_cloud_creds, FORBIDDEN_ADO_SCOPES  # noqa
        from src.security import FORBIDDEN_ENV_VARS
        for v in FORBIDDEN_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        assert check_cloud_creds() == []


class TestPreflightAggregator:
    def test_run_preflight_returns_findings(self, monkeypatch):
        from src.security.preflight import run_preflight, has_blocking, summarize
        monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        findings = run_preflight({})
        # At minimum: ADO + GH + Claude CLI checks.
        assert len(findings) >= 3
        counts = summarize(findings)
        assert sum(counts.values()) == len(findings)
        # Sorted: CRITICAL first.
        levels = [f.level for f in findings]
        assert levels == sorted(levels, key=lambda l: ["CRITICAL", "HIGH", "WARN", "INFO"].index(l))

    def test_has_blocking_only_for_critical(self):
        from src.security.preflight import SecurityFinding, has_blocking
        assert has_blocking([SecurityFinding("X", "CRITICAL", "msg")])
        assert not has_blocking([SecurityFinding("X", "HIGH", "msg")])
        assert not has_blocking([])


# ---------------------------------------------------------------------------
# Filesystem MCP — sensitive paths + write quota
# ---------------------------------------------------------------------------
class TestSensitiveWriteBlocklist:
    @pytest.mark.parametrize("path", [
        ".env", ".env.local", "composer.json", "package.json",
        "pyproject.toml", "Dockerfile", ".gitignore",
        "subdir/.env", "src/composer.json",
    ])
    def test_blocks_sensitive_files(self, path, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_SENSITIVE_WRITE", raising=False)
        from src.mcp.filesystem_server import _check_sensitive_write
        assert _check_sensitive_write(path) is not None

    @pytest.mark.parametrize("path", [
        "src/foo.py", "lib/Bar.php", "README.md", "tests/test_x.py",
    ])
    def test_allows_normal_files(self, path, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_SENSITIVE_WRITE", raising=False)
        from src.mcp.filesystem_server import _check_sensitive_write
        assert _check_sensitive_write(path) is None

    def test_blocks_sensitive_dirs(self, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_SENSITIVE_WRITE", raising=False)
        from src.mcp.filesystem_server import _check_sensitive_write
        assert _check_sensitive_write(".github/workflows/ci.yml") is not None
        assert _check_sensitive_write(".ssh/config") is not None

    def test_allowlist_override(self, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_SENSITIVE_WRITE", "composer.json")
        from src.mcp.filesystem_server import _check_sensitive_write
        assert _check_sensitive_write("composer.json") is None
        # Other sensitive files still blocked.
        assert _check_sensitive_write("package.json") is not None


class TestWriteQuota:
    def test_quota_default_is_30(self, monkeypatch):
        # Re-import to pick up env var (default).
        monkeypatch.delenv("MCP_WRITE_QUOTA", raising=False)
        import importlib
        import src.mcp.filesystem_server as fs
        importlib.reload(fs)
        assert fs.WRITE_QUOTA == 30

    def test_quota_env_override(self, monkeypatch):
        monkeypatch.setenv("MCP_WRITE_QUOTA", "5")
        import importlib
        import src.mcp.filesystem_server as fs
        importlib.reload(fs)
        assert fs.WRITE_QUOTA == 5
