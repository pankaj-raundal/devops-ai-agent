"""Tests for multi-turn tool-use implementation."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agent.implement import (
    MAX_TOOLUSE_CHARS,
    ImplementationAgent,
)


@pytest.fixture
def module_dir(tmp_path):
    """Create a temp module directory with test files."""
    mod = tmp_path / "my_module"
    mod.mkdir()
    (mod / "src").mkdir()
    (mod / "src" / "Service.php").write_text(
        "<?php\nnamespace Drupal\\my_module\\Service;\n\nclass Service {\n  public function run() {}\n}\n"
    )
    (mod / "my_module.module").write_text(
        "<?php\n\n/**\n * @file\n * Module file.\n */\n\nfunction my_module_install() {\n  // install\n}\n"
    )
    (mod / "my_module.info.yml").write_text("name: My Module\ntype: module\ncore_version_requirement: ^10\n")
    (mod / "vendor").mkdir()
    (mod / "vendor" / "secret.txt").write_text("should not be listed")
    return mod


@pytest.fixture
def agent(tmp_path, module_dir):
    """Create an ImplementationAgent with test config."""
    config = {
        "project": {
            "workspace_dir": str(tmp_path),
            "module_path": "my_module",
        },
        "ai_agent": {
            "provider": "copilot",
            "model": "gpt-4o",
            "max_tokens": 4096,
            "temperature": 0.2,
            "trust_level": "full-auto",
        },
    }
    return ImplementationAgent(config)


# --- Tool handler tests ---


class TestToolReadFile:
    def test_read_full_file(self, agent, module_dir):
        result, chars = agent._tool_read_file(
            module_dir, {"path": "src/Service.php"}, 0,
        )
        assert "namespace Drupal" in result
        assert "class Service" in result
        assert chars > 0

    def test_read_line_range(self, agent, module_dir):
        result, chars = agent._tool_read_file(
            module_dir, {"path": "my_module.module", "start_line": 8, "end_line": 10}, 0,
        )
        assert "my_module_install" in result
        assert "Module file" not in result  # line 4, should be excluded
        assert chars > 0

    def test_read_nonexistent_file(self, agent, module_dir):
        result, chars = agent._tool_read_file(
            module_dir, {"path": "nonexistent.php"}, 0,
        )
        assert "not found" in result
        assert chars == 0

    def test_path_traversal_blocked(self, agent, module_dir):
        result, chars = agent._tool_read_file(
            module_dir, {"path": "../../etc/passwd"}, 0,
        )
        assert "outside the module directory" in result
        assert chars == 0

    def test_budget_exhausted(self, agent, module_dir):
        result, chars = agent._tool_read_file(
            module_dir, {"path": "src/Service.php"}, MAX_TOOLUSE_CHARS,
        )
        assert "Budget exhausted" in result
        assert chars == 0

    def test_budget_truncation(self, agent, module_dir):
        # Create a file larger than remaining budget.
        large_file = module_dir / "large.txt"
        large_file.write_text("x" * 5000)

        result, chars = agent._tool_read_file(
            module_dir, {"path": "large.txt"}, MAX_TOOLUSE_CHARS - 100,
        )
        assert "truncated" in result
        assert chars <= 100 + 50  # 100 remaining + truncation message

    def test_directory_not_file(self, agent, module_dir):
        result, chars = agent._tool_read_file(
            module_dir, {"path": "src"}, 0,
        )
        assert "not a file" in result
        assert chars == 0


class TestToolListDirectory:
    def test_list_root(self, agent, module_dir):
        result, chars = agent._tool_list_directory(
            module_dir, {"path": "."},
        )
        assert "src/" in result
        assert "my_module.module" in result
        assert "my_module.info.yml" in result
        assert chars == 0  # list_directory has no budget cost

    def test_list_subdirectory(self, agent, module_dir):
        result, chars = agent._tool_list_directory(
            module_dir, {"path": "src"},
        )
        assert "Service.php" in result
        assert chars == 0

    def test_vendor_excluded(self, agent, module_dir):
        result, _ = agent._tool_list_directory(
            module_dir, {"path": "."},
        )
        assert "vendor" not in result

    def test_hidden_files_excluded(self, agent, module_dir):
        (module_dir / ".hidden").write_text("hidden")
        result, _ = agent._tool_list_directory(
            module_dir, {"path": "."},
        )
        assert ".hidden" not in result

    def test_nonexistent_directory(self, agent, module_dir):
        result, _ = agent._tool_list_directory(
            module_dir, {"path": "nonexistent"},
        )
        assert "not found" in result

    def test_path_traversal_blocked(self, agent, module_dir):
        result, _ = agent._tool_list_directory(
            module_dir, {"path": "../../"},
        )
        assert "outside the module directory" in result


class TestHandleToolCall:
    def test_routes_read_file(self, agent, module_dir):
        result, _ = agent._handle_tool_call(
            "read_file", {"path": "src/Service.php"}, 0,
        )
        assert "class Service" in result

    def test_routes_list_directory(self, agent, module_dir):
        result, _ = agent._handle_tool_call(
            "list_directory", {"path": "."}, 0,
        )
        assert "src/" in result

    def test_unknown_tool(self, agent, module_dir):
        result, chars = agent._handle_tool_call(
            "delete_file", {"path": "src/Service.php"}, 0,
        )
        assert "Unknown tool" in result
        assert chars == 0


class TestToolUseBudget:
    def test_cumulative_budget_tracking(self, agent, module_dir):
        """Multiple reads should accumulate against the budget."""
        _, chars1 = agent._tool_read_file(module_dir, {"path": "src/Service.php"}, 0)
        assert chars1 > 0

        _, chars2 = agent._tool_read_file(module_dir, {"path": "my_module.module"}, chars1)
        assert chars2 > 0

        # Total chars should be tracked.
        total = chars1 + chars2
        assert total > chars1

    def test_line_range_uses_less_budget(self, agent, module_dir):
        """Reading a line range should consume fewer chars than full file."""
        _, full_chars = agent._tool_read_file(
            module_dir, {"path": "my_module.module"}, 0,
        )
        _, range_chars = agent._tool_read_file(
            module_dir, {"path": "my_module.module", "start_line": 1, "end_line": 3}, 0,
        )
        assert range_chars < full_chars
