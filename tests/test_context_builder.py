"""Tests for the context builder."""

from src.agent.context_builder import build_story_context, load_story_context, save_story_context
from src.integrations.azure_devops import WorkItem


def test_build_story_context():
    item = WorkItem(
        id=123,
        title="Test Story",
        work_item_type="User Story",
        state="Active",
        description="Implement foo feature.",
        acceptance_criteria="AC: foo works",
        tags="auto",
        comments=[{"date": "2025-01-01", "text": "Comment 1"}, {"date": "2025-01-02", "text": "Comment 2"}],
    )
    config = {"project": {"name": "test", "module_path": "web/modules/test"}, "local_env": {"drush_prefix": "drush"}}
    context = build_story_context(item, config)
    assert "# Current Work Item" in context
    assert "Test Story" in context
    assert "Implement foo feature." in context
    assert "Comment 1" in context


def test_save_and_load(tmp_path):
    item = WorkItem(
        id=456,
        title="Save Test",
        work_item_type="Bug",
        state="New",
        description="Desc",
        acceptance_criteria="AC",
        tags="",
        comments=[],
    )
    config = {
        "project": {"name": "test", "workspace_dir": str(tmp_path), "module_path": "web/modules/test"},
        "local_env": {"drush_prefix": "drush"},
    }
    path = save_story_context(item, config)
    assert path.exists()
    loaded = load_story_context(config)
    assert loaded is not None
    assert "Save Test" in loaded
