"""Tests for Phase 4: SQLite history, history-aware retry, batch summary, feedback."""

import json
from datetime import datetime, timezone

from src.history import (
    _get_connection,
    save_run_record,
    load_run_history,
    load_runs_for_story,
    build_history_context,
    save_feedback,
    load_feedback_for_story,
    generate_batch_summary,
    migrate_from_json,
    _db_path,
)


def _cfg(trust="cautious", provider="copilot"):
    return {"ai_agent": {"trust_level": trust, "provider": provider}}


# ── Schema ──


def test_schema_creates_tables():
    conn = _get_connection()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r["name"] for r in tables}
    conn.close()
    assert "runs" in names
    assert "feedback" in names


# ── save / load run records ──


def test_save_and_load_record():
    row_id = save_run_record(_cfg(), {
        "work_item_id": 9001,
        "failed_stage": None,
        "method": "copilot-plan(ctx)",
        "branch": "feature/9001-test",
    })
    assert row_id > 0

    records = load_run_history(_cfg(), limit=5)
    assert any(r["work_item_id"] == 9001 for r in records)


def test_load_runs_for_story():
    wid = 9002
    save_run_record(_cfg(), {"work_item_id": wid, "failed_stage": "test", "error": "phpunit failed"})
    save_run_record(_cfg(), {"work_item_id": wid, "failed_stage": None, "method": "copilot-plan"})
    save_run_record(_cfg(), {"work_item_id": 9999, "failed_stage": None})

    runs = load_runs_for_story(wid)
    assert len(runs) >= 2
    assert all(r["work_item_id"] == wid for r in runs)


def test_record_stores_trust_and_provider():
    row_id = save_run_record(
        _cfg(trust="full-auto", provider="anthropic"),
        {"work_item_id": 9003, "failed_stage": None},
    )
    conn = _get_connection()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    assert row["trust_level"] == "full-auto"
    assert row["provider"] == "anthropic"


# ── #15 History-aware retry ──


def test_build_history_context_includes_errors():
    wid = 9010
    save_run_record(_cfg(), {"work_item_id": wid, "failed_stage": "test", "error": "AssertionError"})
    save_run_record(_cfg(), {"work_item_id": wid, "failed_stage": None, "method": "copilot-plan"})

    ctx = build_history_context(_cfg(), wid)
    assert "Previous Run History" in ctx
    assert "AssertionError" in ctx
    assert "avoid repeating mistakes" in ctx


def test_build_history_context_empty_for_new_story():
    ctx = build_history_context(_cfg(), 99999)
    assert ctx == ""


def test_build_history_context_includes_feedback():
    wid = 9011
    run_id = save_run_record(_cfg(), {"work_item_id": wid, "failed_stage": "test"})
    save_feedback(run_id, "src/Service.php", action="edit",
                  diff="-old code\n+new code", comment="Wrong method signature")

    ctx = build_history_context(_cfg(), wid)
    assert "User corrections" in ctx
    assert "Service.php" in ctx
    assert "Wrong method signature" in ctx


# ── #16 Rejection feedback ──


def test_save_and_load_feedback():
    wid = 9020
    run_id = save_run_record(_cfg(), {"work_item_id": wid, "failed_stage": None})
    fb_id = save_feedback(run_id, "src/app.py", action="edit",
                          diff="-x = 1\n+x = 2", comment="should be 2")
    assert fb_id > 0

    feedback = load_feedback_for_story(wid)
    assert len(feedback) >= 1
    assert feedback[0]["file_path"] == "src/app.py"
    assert feedback[0]["action"] == "edit"
    assert "-x = 1" in feedback[0]["diff"]


# ── #13 Batch summary ──


def test_generate_batch_summary():
    wid1, wid2 = 9030, 9031
    id1 = save_run_record(_cfg(), {"work_item_id": wid1, "failed_stage": None, "method": "copilot-plan"})
    id2 = save_run_record(_cfg(), {"work_item_id": wid2, "failed_stage": "test", "error": "lint failed"})

    summary = generate_batch_summary(run_ids=[id1, id2])
    assert "Batch Run Summary" in summary
    assert "PASS" in summary
    assert "lint failed" in summary
    assert f"#{wid1}" in summary
    assert f"#{wid2}" in summary


def test_generate_batch_summary_since():
    ts = datetime.now(timezone.utc).isoformat()
    save_run_record(_cfg(), {"work_item_id": 9040, "failed_stage": None})

    summary = generate_batch_summary(since=ts)
    assert "9040" in summary


def test_generate_batch_summary_empty():
    summary = generate_batch_summary(since="2099-01-01T00:00:00")
    assert "No runs found" in summary


# ── Migration ──


def test_migrate_from_json(tmp_path, monkeypatch):
    import src.history as hist_mod
    monkeypatch.setattr(hist_mod, "_DATA_DIR", tmp_path)

    json_file = tmp_path / ".pipeline-history.json"
    json_file.write_text(json.dumps([
        {"work_item_id": 8001, "failed_stage": None, "method": "api"},
        {"work_item_id": 8002, "failed_stage": "test", "error": "fail"},
    ]))

    count = migrate_from_json(_cfg())
    assert count == 2
    assert not json_file.exists()
    assert (tmp_path / ".pipeline-history.json.migrated").exists()
