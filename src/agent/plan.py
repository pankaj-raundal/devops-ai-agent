"""Implementation plan — structured output from AI planning phase."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.plan")


@dataclass
class FileChange:
    """A single file change proposed by the AI."""

    path: str  # Relative to module root.
    action: str  # "create", "modify", "delete"
    description: str  # What is being changed and why.
    content: str = ""  # Code content (full file for create, new code only for modify).
    merge_strategy: str = "append"  # "append" (add to existing) or "replace" (overwrite file).
    approved: bool = False  # Set during review.

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "action": self.action,
            "description": self.description,
            "content_preview": self.content[:500] + ("..." if len(self.content) > 500 else ""),
            "content_length": len(self.content),
            "merge_strategy": self.merge_strategy,
            "approved": self.approved,
        }


@dataclass
class ImplementationPlan:
    """Structured implementation plan from AI — reviewed before execution."""

    summary: str = ""
    approach: str = ""
    file_changes: list[FileChange] = field(default_factory=list)
    testing_steps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    approved: bool = False  # Overall plan approval.

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "approach": self.approach,
            "file_changes": [f.to_dict() for f in self.file_changes],
            "testing_steps": self.testing_steps,
            "risks": self.risks,
            "total_files": len(self.file_changes),
            "approved": self.approved,
        }

    def to_markdown(self) -> str:
        """Render a human-readable markdown summary of the plan."""
        lines = [
            "## Implementation Plan",
            "",
            f"**Summary:** {self.summary}",
            "",
            f"**Approach:** {self.approach}",
            "",
            f"### File Changes ({len(self.file_changes)})",
            "",
        ]
        for i, fc in enumerate(self.file_changes, 1):
            status = "✅" if fc.approved else "⬜"
            lines.append(f"{status} **{i}. [{fc.action.upper()}]** `{fc.path}`")
            lines.append(f"   {fc.description}")
            lines.append("")

        if self.risks:
            lines.append("### Risks")
            for r in self.risks:
                lines.append(f"- ⚠ {r}")
            lines.append("")

        if self.testing_steps:
            lines.append("### Testing Steps")
            for t in self.testing_steps:
                lines.append(f"- {t}")
            lines.append("")

        return "\n".join(lines)


# ── JSON → Plan parsing ──

# The AI is prompted to return JSON matching this structure.
PLAN_JSON_SCHEMA = """\
{
  "summary": "Brief summary of what the implementation does",
  "approach": "High-level approach description",
  "file_changes": [
    {
      "path": "relative/path/to/file.php",
      "action": "create | modify | delete",
      "description": "What is being changed and why",
      "content": "For create: full file content. For modify: ONLY the new code to add (NOT the full file).",
      "merge_strategy": "append | replace  (append = add to end of existing file, replace = overwrite entire file)"
    }
  ],
  "testing_steps": ["Step 1", "Step 2"],
  "risks": ["Risk 1", "Risk 2"]
}"""


def parse_plan_response(response_text: str) -> ImplementationPlan:
    """Parse AI response into a structured ImplementationPlan.

    The AI should return JSON, but we also handle a JSON block embedded in markdown.
    """
    text = response_text.strip()

    # Extract JSON from markdown code fences (handles ```json, ```JSON, ``` with extra whitespace).
    import re
    fence_match = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()
    elif text.startswith("```"):
        # Fallback: strip leading/trailing fences.
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("AI response is not valid JSON — wrapping as unstructured plan.")
        return ImplementationPlan(
            summary="AI returned unstructured response (manual review required)",
            approach=response_text[:1000],
            file_changes=[],
            risks=["AI response could not be parsed as a structured plan"],
        )

    file_changes = []
    for fc_data in data.get("file_changes", []):
        file_changes.append(FileChange(
            path=fc_data.get("path", ""),
            action=fc_data.get("action", "modify"),
            description=fc_data.get("description", ""),
            content=fc_data.get("content", ""),
            merge_strategy=fc_data.get("merge_strategy", "append"),
        ))

    # Validate merge strategies.
    for fc in file_changes:
        if fc.merge_strategy not in ("append", "replace"):
            logger.warning("Invalid merge_strategy '%s' for %s — defaulting to 'append'.", fc.merge_strategy, fc.path)
            fc.merge_strategy = "append"

    return ImplementationPlan(
        summary=data.get("summary", ""),
        approach=data.get("approach", ""),
        file_changes=file_changes,
        testing_steps=data.get("testing_steps", []),
        risks=data.get("risks", []),
    )


def apply_plan(plan: ImplementationPlan, workspace_dir: Path, module_path: str) -> dict:
    """Apply approved file changes from the plan to disk.

    Only applies changes where file_change.approved is True.
    Uses smart merge for append strategy (PHP/Python-aware insertion).
    Auto-upgrades small files (<500 lines) to replace when content looks complete.
    Returns a summary dict.
    """
    module_dir = workspace_dir / module_path
    if not module_dir.exists():
        raise FileNotFoundError(
            f"Module directory does not exist: {module_dir}. "
            f"Check project.workspace_dir and project.module_path in config."
        )
    applied = []
    skipped = []

    for fc in plan.file_changes:
        if not fc.approved:
            skipped.append(fc.path)
            logger.info("Skipped (not approved): %s", fc.path)
            continue

        target = module_dir / fc.path
        try:
            if fc.action == "delete":
                if target.exists():
                    target.unlink()
                    applied.append({"path": fc.path, "action": "deleted"})
                    logger.info("Deleted: %s", fc.path)
                else:
                    skipped.append(fc.path)
                    logger.warning("Delete target not found: %s", fc.path)
            elif fc.action == "create":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(fc.content)
                applied.append({"path": fc.path, "action": "created"})
                logger.info("Created: %s", fc.path)
            elif fc.action == "modify":
                target.parent.mkdir(parents=True, exist_ok=True)
                if fc.merge_strategy == "replace" or not target.exists():
                    # Replace strategy or file doesn't exist — write full content.
                    target.write_text(fc.content)
                    applied.append({"path": fc.path, "action": "modified (replaced)"})
                    logger.info("Replaced: %s", fc.path)
                else:
                    # Append strategy — use smart merge.
                    existing = target.read_text()
                    existing_lines = existing.count("\n") + 1

                    # Auto-upgrade: small file + content looks complete → replace.
                    if existing_lines < 500 and _looks_like_complete_file(fc.content, target.suffix):
                        target.write_text(fc.content)
                        applied.append({"path": fc.path, "action": "modified (auto-replaced)"})
                        logger.info("Auto-replaced small file (%d lines): %s", existing_lines, fc.path)
                    else:
                        merged = _smart_merge(existing, fc.content, target.suffix)
                        target.write_text(merged)
                        applied.append({"path": fc.path, "action": "modified (smart-merged)"})
                        logger.info("Smart-merged into: %s", fc.path)
            else:
                skipped.append(fc.path)
                logger.warning("Unknown action '%s' for %s", fc.action, fc.path)
        except Exception as e:
            logger.error("Failed to apply %s to %s: %s", fc.action, fc.path, e)
            skipped.append(fc.path)

    return {
        "applied": applied,
        "skipped": skipped,
        "total_applied": len(applied),
        "total_skipped": len(skipped),
    }


def _looks_like_complete_file(content: str, suffix: str) -> bool:
    """Heuristic: does the content look like a complete file rather than a code fragment?"""
    content = content.strip()
    if not content:
        return False

    if suffix in (".php", ".module", ".install", ".inc", ".theme"):
        return content.startswith("<?php")
    elif suffix == ".py":
        return (
            content.startswith('"""')
            or content.startswith("'''")
            or content.startswith("from ")
            or content.startswith("import ")
            or content.startswith("#!")
        )
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        return content.startswith("import ") or content.startswith("export ")
    elif suffix == ".java":
        return content.startswith("package ") or content.startswith("import ")
    elif suffix == ".cs":
        return content.startswith("using ") or content.startswith("namespace ")
    return False


def _smart_merge(existing: str, new_code: str, suffix: str) -> str:
    """Merge new code into an existing file at an intelligent position.

    PHP: inserts before the last closing brace (end of class).
    Python: inserts before ``if __name__`` block, or at end.
    Default: appends to end of file.
    """
    import re

    # PHP: insert before last `}` at column 0 (likely class/interface end).
    if suffix in (".php", ".module", ".install", ".inc", ".theme"):
        lines = existing.rstrip("\n").split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "}" and (not lines[i] or lines[i][0] == "}"):
                lines.insert(i, "\n" + new_code.strip() + "\n")
                return "\n".join(lines) + "\n"

    # Python: insert before `if __name__` guard, or at end.
    if suffix == ".py":
        lines = existing.rstrip("\n").split("\n")
        for i, line in enumerate(lines):
            if re.match(r'if\s+__name__\s*==\s*["\']__main__["\']', line):
                lines.insert(i, "\n" + new_code.strip() + "\n")
                return "\n".join(lines) + "\n"

    # Default: append.
    return existing.rstrip("\n") + "\n\n" + new_code.lstrip("\n")
