"""Tests for MCP servers — filesystem, azure_devops, git."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ==== Filesystem Server Tests ====


class TestFilesystemServer:
    """Tests for src.mcp.filesystem_server tools."""

    def setup_method(self):
        """Create a temporary module directory for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.orig_module = os.environ.get("MODULE_PATH")
        self.orig_workspace = os.environ.get("WORKSPACE_PATH")
        os.environ["MODULE_PATH"] = self.tmpdir
        os.environ["WORKSPACE_PATH"] = self.tmpdir

        # Reload module to pick up new env vars.
        import src.mcp.filesystem_server as fs
        fs.MODULE_PATH = Path(self.tmpdir).resolve()
        fs.WORKSPACE_PATH = Path(self.tmpdir).resolve()
        fs.reset_budget()
        self.fs = fs

    def teardown_method(self):
        """Restore env vars and clean up."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self.orig_module is not None:
            os.environ["MODULE_PATH"] = self.orig_module
        else:
            os.environ.pop("MODULE_PATH", None)
        if self.orig_workspace is not None:
            os.environ["WORKSPACE_PATH"] = self.orig_workspace
        else:
            os.environ.pop("WORKSPACE_PATH", None)

    def test_read_file_success(self):
        """read_file returns file content."""
        test_file = Path(self.tmpdir) / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        result = self.fs.read_file("test.txt")
        assert "line1" in result
        assert "line3" in result

    def test_read_file_line_range(self):
        """read_file with line range returns subset."""
        test_file = Path(self.tmpdir) / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        result = self.fs.read_file("test.txt", start_line=2, end_line=3)
        assert "line2" in result
        assert "line3" in result
        assert "line1" not in result
        assert "line4" not in result

    def test_read_file_not_found(self):
        """read_file returns error for missing file."""
        result = self.fs.read_file("nonexistent.txt")
        assert "Error" in result or "not found" in result.lower()

    def test_read_file_sandbox_escape(self):
        """read_file blocks path traversal attempts."""
        with pytest.raises(ValueError, match="sandbox"):
            self.fs.read_file("../../etc/passwd")

    def test_read_file_budget_exceeded(self):
        """read_file stops after budget is exceeded."""
        self.fs._chars_read = self.fs.MAX_READ_CHARS + 1
        result = self.fs.read_file("test.txt")
        assert "Budget exceeded" in result

    def test_list_directory(self):
        """list_directory shows files with sizes."""
        (Path(self.tmpdir) / "a.txt").write_text("hello")
        (Path(self.tmpdir) / "subdir").mkdir()

        result = self.fs.list_directory(".")
        assert "a.txt" in result
        assert "subdir/" in result

    def test_list_directory_empty(self):
        """list_directory shows empty message for empty dirs."""
        (Path(self.tmpdir) / "empty").mkdir()
        result = self.fs.list_directory("empty")
        assert "empty" in result.lower()

    def test_list_directory_not_found(self):
        """list_directory returns error for non-existent dir."""
        result = self.fs.list_directory("nonexistent")
        assert "Error" in result or "not a directory" in result.lower()

    def test_write_file_new(self):
        """write_file creates a new file."""
        result = self.fs.write_file("new.txt", "hello world")
        assert "Successfully" in result
        assert (Path(self.tmpdir) / "new.txt").read_text() == "hello world"

    def test_write_file_backup(self):
        """write_file creates .bak backup of existing files."""
        original = Path(self.tmpdir) / "existing.txt"
        original.write_text("original content")

        self.fs.write_file("existing.txt", "new content")
        assert original.read_text() == "new content"
        assert (Path(self.tmpdir) / "existing.txt.bak").read_text() == "original content"

    def test_write_file_creates_dirs(self):
        """write_file creates parent directories."""
        self.fs.write_file("deep/nested/file.txt", "content")
        assert (Path(self.tmpdir) / "deep" / "nested" / "file.txt").read_text() == "content"

    def test_write_file_sandbox_escape(self):
        """write_file blocks path traversal."""
        with pytest.raises(ValueError, match="sandbox"):
            self.fs.write_file("../../etc/evil", "bad")

    def test_run_command_allowed(self):
        """run_command runs allowed commands."""
        # Create a pyproject.toml so it detects python project.
        (Path(self.tmpdir) / "pyproject.toml").write_text("[tool.pytest]")

        result = self.fs.run_command("test")
        # Will likely fail since there are no actual tests, but should not raise.
        assert "Command 'test'" in result

    def test_run_command_blocked(self):
        """run_command rejects unknown commands."""
        result = self.fs.run_command("rm -rf /")
        assert "not allowed" in result.lower()


# ==== Azure DevOps Server Tests ====


class TestAzureDevOpsServer:
    """Tests for src.mcp.azure_devops_server tools."""

    def setup_method(self):
        """Set env vars and reset limits."""
        self.orig_org = os.environ.get("AZURE_DEVOPS_ORG")
        self.orig_project = os.environ.get("AZURE_DEVOPS_PROJECT")
        os.environ["AZURE_DEVOPS_ORG"] = "https://dev.azure.com/test-org"
        os.environ["AZURE_DEVOPS_PROJECT"] = "TestProject"

        import src.mcp.azure_devops_server as ado
        ado.ORG = "https://dev.azure.com/test-org"
        ado.PROJECT = "TestProject"
        ado.reset_limits()
        self.ado = ado

    def teardown_method(self):
        if self.orig_org is not None:
            os.environ["AZURE_DEVOPS_ORG"] = self.orig_org
        else:
            os.environ.pop("AZURE_DEVOPS_ORG", None)
        if self.orig_project is not None:
            os.environ["AZURE_DEVOPS_PROJECT"] = self.orig_project
        else:
            os.environ.pop("AZURE_DEVOPS_PROJECT", None)

    @patch("src.mcp.azure_devops_server._run_az_json")
    def test_get_work_item_success(self, mock_az):
        """get_work_item returns formatted markdown."""
        mock_az.return_value = {
            "fields": {
                "System.Title": "Test Story",
                "System.State": "Active",
                "System.WorkItemType": "User Story",
                "System.Description": "<p>Test description</p>",
                "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>AC 1</p>",
                "System.Tags": "auto",
            }
        }
        result = self.ado.get_work_item(12345)
        assert "Test Story" in result
        assert "Active" in result
        assert "Test description" in result

    @patch("src.mcp.azure_devops_server._run_az_json")
    def test_get_work_item_error(self, mock_az):
        """get_work_item handles errors gracefully."""
        mock_az.side_effect = RuntimeError("az failed")
        result = self.ado.get_work_item(99999)
        assert "Error" in result

    def test_get_work_item_no_org(self):
        """get_work_item fails gracefully without org."""
        self.ado.ORG = ""
        result = self.ado.get_work_item(12345)
        assert "AZURE_DEVOPS_ORG not set" in result

    @patch("src.mcp.azure_devops_server.subprocess.run")
    def test_add_comment_success(self, mock_run):
        """add_comment posts and tracks count."""
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        result = self.ado.add_comment(12345, "Test comment")
        assert "posted" in result.lower()
        assert "1/5" in result

    @patch("src.mcp.azure_devops_server.subprocess.run")
    def test_add_comment_rate_limit(self, mock_run):
        """add_comment enforces rate limit."""
        mock_run.return_value = MagicMock(returncode=0, stdout="{}")
        for i in range(5):
            self.ado.add_comment(12345, f"Comment {i}")
        result = self.ado.add_comment(12345, "One too many")
        assert "limit reached" in result.lower()


# ==== Git Server Tests ====


class TestGitServer:
    """Tests for src.mcp.git_server tools."""

    def setup_method(self):
        """Create a temp git repo."""
        self.tmpdir = tempfile.mkdtemp()
        # Init a git repo.
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.tmpdir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.tmpdir, capture_output=True)
        # Create initial commit.
        (Path(self.tmpdir) / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.tmpdir, capture_output=True)

        import src.mcp.git_server as gs
        gs.WORKSPACE = Path(self.tmpdir).resolve()
        gs.BASE_BRANCH = "master"
        self.gs = gs

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_git_status_clean(self):
        """git_status reports clean tree."""
        result = self.gs.git_status()
        assert "clean" in result.lower() or "no changes" in result.lower()

    def test_git_status_dirty(self):
        """git_status shows modified files."""
        (Path(self.tmpdir) / "new.txt").write_text("new file")
        result = self.gs.git_status()
        assert "new.txt" in result

    def test_git_log(self):
        """git_log shows commit history."""
        result = self.gs.git_log(count=5)
        assert "init" in result

    def test_git_diff_no_changes(self):
        """git_diff with no changes returns no diff message."""
        result = self.gs.git_diff()
        assert "no diff" in result.lower() or "no changes" in result.lower()

    def test_get_changed_files_no_changes(self):
        """get_changed_files with no changes returns empty."""
        result = self.gs.get_changed_files()
        assert "no files changed" in result.lower() or "0 files" in result.lower()

    def test_git_log_max_cap(self):
        """git_log caps count at 50."""
        result = self.gs.git_log(count=100)
        # Should not error, just caps at 50.
        assert "init" in result


# ==== MCP Config Generation Tests ====


class TestMCPConfig:
    """Tests for src.mcp.config."""

    def test_generate_config(self, tmp_path):
        """generate_mcp_config creates valid JSON."""
        from src.mcp.config import MCP_CONFIG_PATH, generate_mcp_config

        config = {
            "azure_devops": {
                "organization": "https://dev.azure.com/test",
                "project": "TestProject",
            },
            "project": {
                "base_branch": "main",
            },
        }

        with patch("src.mcp.config.MCP_CONFIG_PATH", tmp_path / ".mcp.json"):
            from src.mcp import config as mcp_cfg
            mcp_cfg.MCP_CONFIG_PATH = tmp_path / ".mcp.json"
            result = mcp_cfg.generate_mcp_config("/tmp/module", "/tmp/workspace", config)

            assert result.exists()
            data = json.loads(result.read_text())
            assert "mcpServers" in data
            assert "filesystem" in data["mcpServers"]
            assert "azure-devops" in data["mcpServers"]
            assert "git" in data["mcpServers"]
            assert data["mcpServers"]["filesystem"]["env"]["MODULE_PATH"] == "/tmp/module"

    def test_cleanup_config(self, tmp_path):
        """cleanup_mcp_config removes the file."""
        from src.mcp import config as mcp_cfg
        mcp_cfg.MCP_CONFIG_PATH = tmp_path / ".mcp.json"
        mcp_cfg.MCP_CONFIG_PATH.write_text("{}")

        mcp_cfg.cleanup_mcp_config()
        assert not mcp_cfg.MCP_CONFIG_PATH.exists()

    def test_get_config_path_exists(self, tmp_path):
        """get_mcp_config_path returns path when file exists."""
        from src.mcp import config as mcp_cfg
        mcp_cfg.MCP_CONFIG_PATH = tmp_path / ".mcp.json"
        mcp_cfg.MCP_CONFIG_PATH.write_text("{}")

        assert mcp_cfg.get_mcp_config_path() is not None

    def test_get_config_path_missing(self, tmp_path):
        """get_mcp_config_path returns None when file missing."""
        from src.mcp import config as mcp_cfg
        mcp_cfg.MCP_CONFIG_PATH = tmp_path / ".mcp-nonexistent.json"

        assert mcp_cfg.get_mcp_config_path() is None
