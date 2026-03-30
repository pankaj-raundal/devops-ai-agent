"""Tests for Phase 2: context-aware implementation features."""

from pathlib import Path

from src.agent.plan import (
    FileChange,
    ImplementationPlan,
    apply_plan,
    _looks_like_complete_file,
    _smart_merge,
)
from src.agent.implement import ImplementationAgent, MAX_CONTEXT_CHARS


# ── _looks_like_complete_file ──


def test_complete_file_php():
    assert _looks_like_complete_file("<?php\nnamespace App;", ".php") is True
    assert _looks_like_complete_file("<?php\n// module", ".module") is True


def test_fragment_php():
    assert _looks_like_complete_file("public function foo() {}", ".php") is False


def test_complete_file_python():
    assert _looks_like_complete_file('"""Docstring."""\nimport os', ".py") is True
    assert _looks_like_complete_file("from pathlib import Path", ".py") is True
    assert _looks_like_complete_file("import json", ".py") is True
    assert _looks_like_complete_file("#!/usr/bin/env python3", ".py") is True


def test_fragment_python():
    assert _looks_like_complete_file("def helper():\n    pass", ".py") is False


def test_complete_file_typescript():
    assert _looks_like_complete_file("import React from 'react';", ".ts") is True
    assert _looks_like_complete_file("export default App;", ".tsx") is True


def test_complete_file_java():
    assert _looks_like_complete_file("package com.example;", ".java") is True


def test_complete_file_csharp():
    assert _looks_like_complete_file("using System;", ".cs") is True
    assert _looks_like_complete_file("namespace App {", ".cs") is True


def test_empty_content():
    assert _looks_like_complete_file("", ".py") is False
    assert _looks_like_complete_file("   ", ".php") is False


# ── _smart_merge ──


def test_smart_merge_php_inserts_before_closing_brace():
    existing = (
        "<?php\n"
        "namespace App;\n"
        "\n"
        "class MyService {\n"
        "  public function existing() {}\n"
        "}\n"
    )
    new_code = "  public function newMethod() { return 42; }"
    merged = _smart_merge(existing, new_code, ".php")
    # New code should appear before the closing `}`
    assert "newMethod" in merged
    closing_pos = merged.rfind("}")
    new_pos = merged.find("newMethod")
    assert new_pos < closing_pos


def test_smart_merge_python_before_main():
    existing = (
        "import os\n"
        "\n"
        "def existing():\n"
        "    pass\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    existing()\n"
    )
    new_code = "def new_func():\n    return 42"
    merged = _smart_merge(existing, new_code, ".py")
    assert "new_func" in merged
    main_pos = merged.find("if __name__")
    new_pos = merged.find("new_func")
    assert new_pos < main_pos


def test_smart_merge_default_appends():
    existing = "line1\nline2\n"
    new_code = "line3\n"
    merged = _smart_merge(existing, new_code, ".txt")
    assert merged.endswith("line3\n")
    assert "line1" in merged
    assert "line2" in merged


def test_smart_merge_python_no_main_appends():
    existing = "import os\n\ndef foo():\n    pass\n"
    new_code = "def bar():\n    return 1"
    merged = _smart_merge(existing, new_code, ".py")
    assert "bar" in merged
    assert merged.index("foo") < merged.index("bar")


# ── apply_plan: auto-replace for small files ──


def test_apply_plan_auto_replace_small_php(tmp_path):
    module_dir = tmp_path / "mymod"
    module_dir.mkdir()
    target = module_dir / "Service.php"
    target.write_text("<?php\nclass Service {\n  function old() {}\n}\n")

    plan = ImplementationPlan(
        summary="test",
        file_changes=[
            FileChange(
                path="Service.php",
                action="modify",
                description="update service",
                content="<?php\nclass Service {\n  function old() {}\n  function newMethod() {}\n}\n",
                merge_strategy="append",  # AI said append — but content is complete.
                approved=True,
            )
        ],
    )
    result = apply_plan(plan, tmp_path, "mymod")
    assert result["total_applied"] == 1
    assert "auto-replaced" in result["applied"][0]["action"]
    written = target.read_text()
    assert "newMethod" in written
    # Should NOT have double content (old appended to old).
    assert written.count("<?php") == 1


def test_apply_plan_smart_merge_fragment(tmp_path):
    module_dir = tmp_path / "mymod"
    module_dir.mkdir()
    target = module_dir / "Service.php"
    target.write_text("<?php\nclass Service {\n  function old() {}\n}\n")

    plan = ImplementationPlan(
        summary="test",
        file_changes=[
            FileChange(
                path="Service.php",
                action="modify",
                description="add method",
                content="  public function extra() { return 1; }",
                merge_strategy="append",
                approved=True,
            )
        ],
    )
    result = apply_plan(plan, tmp_path, "mymod")
    assert result["total_applied"] == 1
    assert "smart-merged" in result["applied"][0]["action"]
    written = target.read_text()
    assert "extra" in written
    assert "old" in written  # existing code preserved


def test_apply_plan_replace_strategy_unchanged(tmp_path):
    module_dir = tmp_path / "mymod"
    module_dir.mkdir()
    target = module_dir / "new.py"
    target.write_text("# old\n")

    plan = ImplementationPlan(
        summary="test",
        file_changes=[
            FileChange(
                path="new.py",
                action="modify",
                description="replace",
                content="# brand new content\n",
                merge_strategy="replace",
                approved=True,
            )
        ],
    )
    result = apply_plan(plan, tmp_path, "mymod")
    assert result["total_applied"] == 1
    assert "replaced" in result["applied"][0]["action"]
    assert target.read_text() == "# brand new content\n"


# ── _read_file_contents ──


def test_read_file_contents_basic(tmp_path):
    module_dir = tmp_path / "mod"
    module_dir.mkdir()
    (module_dir / "a.py").write_text("print('a')\n")
    (module_dir / "b.py").write_text("print('b')\n")

    config = {
        "project": {"workspace_dir": str(tmp_path), "module_path": "mod"},
        "ai_agent": {"provider": "copilot", "require_consent": False},
    }
    agent = ImplementationAgent(config)
    content, counts = agent._read_file_contents(["a.py", "b.py"])
    assert "a.py" in content
    assert "b.py" in content
    assert counts["a.py"] == 2  # "print('a')\n" → 1 newline + 1 = 2 lines
    assert counts["b.py"] == 2


def test_read_file_contents_path_traversal(tmp_path):
    module_dir = tmp_path / "mod"
    module_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("supersecret")

    config = {
        "project": {"workspace_dir": str(tmp_path), "module_path": "mod"},
        "ai_agent": {"provider": "copilot", "require_consent": False},
    }
    agent = ImplementationAgent(config)
    content, counts = agent._read_file_contents(["../secret.txt"])
    # Should NOT include the file outside module dir.
    assert "supersecret" not in content
    assert len(counts) == 0


def test_read_file_contents_budget(tmp_path):
    module_dir = tmp_path / "mod"
    module_dir.mkdir()
    # Create a file larger than the budget.
    big_content = "x" * (MAX_CONTEXT_CHARS + 1000)
    (module_dir / "big.py").write_text(big_content)
    (module_dir / "small.py").write_text("y = 1\n")

    config = {
        "project": {"workspace_dir": str(tmp_path), "module_path": "mod"},
        "ai_agent": {"provider": "copilot", "require_consent": False},
    }
    agent = ImplementationAgent(config)
    content, counts = agent._read_file_contents(["big.py", "small.py"])
    # big.py should be truncated; small.py may be skipped due to budget.
    assert "big.py" in counts
    assert "(truncated)" in content
