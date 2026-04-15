"""Run history — SQLite-backed persistent storage for pipeline run records.

Replaces the old 10-entry JSON file with a queryable database that supports
unlimited history, per-story queries, and rejection feedback capture.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.history")

_DATA_DIR = Path(__file__).resolve().parent.parent / ".dai"
_DB_FILENAME = "history.db"


def _db_path() -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR / _DB_FILENAME


def _get_connection() -> sqlite3.Connection:
    """Open (or create) the SQLite database and ensure the schema exists."""
    path = _db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id    INTEGER NOT NULL,
            timestamp       TEXT    NOT NULL,
            failed_stage    TEXT,               -- NULL = success
            method          TEXT    DEFAULT '',
            error           TEXT    DEFAULT '',
            ai_output       TEXT    DEFAULT '',
            review_verdict  TEXT    DEFAULT '',
            branch          TEXT    DEFAULT '',
            fix_attempts    INTEGER DEFAULT 0,
            trust_level     TEXT    DEFAULT '',
            provider        TEXT    DEFAULT '',
            extra           TEXT    DEFAULT '{}'  -- JSON blob for future fields
        );

        CREATE INDEX IF NOT EXISTS idx_runs_work_item
            ON runs(work_item_id);

        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL REFERENCES runs(id),
            file_path   TEXT    NOT NULL,
            action      TEXT    NOT NULL DEFAULT 'edit',   -- edit, reject, approve
            diff        TEXT    DEFAULT '',                 -- user's corrections
            comment     TEXT    DEFAULT '',
            timestamp   TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_feedback_run
            ON feedback(run_id);

        CREATE TABLE IF NOT EXISTS token_usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            story_id          INTEGER,            -- work item ID (NULL for non-story calls)
            stage             TEXT    NOT NULL,    -- analyze, implement, fix-N, review
            provider          TEXT    NOT NULL,    -- copilot, anthropic, openai
            model             TEXT    DEFAULT '',
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens      INTEGER DEFAULT 0,
            timestamp         TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_token_usage_story
            ON token_usage(story_id);

        CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp
            ON token_usage(timestamp);
    """)
    conn.commit()


# ── Save / Load ──


def save_run_record(config: dict, record: dict) -> int:
    """Insert a run record and return the new row ID."""
    conn = _get_connection()
    try:
        ai_cfg = config.get("ai_agent", {})
        cur = conn.execute(
            """INSERT INTO runs
               (work_item_id, timestamp, failed_stage, method, error,
                ai_output, review_verdict, branch, fix_attempts,
                trust_level, provider, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.get("work_item_id", 0),
                datetime.now(timezone.utc).isoformat(),
                record.get("failed_stage"),
                record.get("method", ""),
                record.get("error", "")[:2000],
                record.get("ai_output", "")[:2000],
                record.get("review_verdict", ""),
                record.get("branch", ""),
                record.get("fix_attempts", 0),
                ai_cfg.get("trust_level", ""),
                ai_cfg.get("provider", ""),
                json.dumps({k: v for k, v in record.items()
                            if k not in ("work_item_id", "failed_stage", "method",
                                         "error", "ai_output", "review_verdict",
                                         "branch", "fix_attempts")}),
            ),
        )
        conn.commit()
        row_id = cur.lastrowid
        logger.info("Run record saved (id=%d, work_item=%s).", row_id, record.get("work_item_id"))
        return row_id
    finally:
        conn.close()


def load_run_history(config: dict, limit: int = 100) -> list[dict]:
    """Load recent pipeline run records (newest first)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_runs_for_story(work_item_id: int) -> list[dict]:
    """Load all run records for a specific work item (oldest first)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM runs WHERE work_item_id = ? ORDER BY id ASC",
            (work_item_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def build_history_context(config: dict, work_item_id: int) -> str:
    """Build a markdown section summarizing previous runs for the same story.

    Includes past errors and any rejection feedback to help the AI avoid
    repeating the same mistakes (#15 history-aware retry).
    """
    runs = load_runs_for_story(work_item_id)
    if not runs:
        return ""

    lines = ["## Previous Run History\n"]
    conn = _get_connection()
    try:
        for i, run in enumerate(runs, 1):
            ts = run.get("timestamp", "unknown")
            failed_stage = run.get("failed_stage")
            error = run.get("error", "")
            method = run.get("method", "")
            lines.append(f"### Run {i} ({ts})")
            lines.append(f"- **Result:** {'FAILED at ' + failed_stage if failed_stage else 'SUCCESS'}")
            if method:
                lines.append(f"- **Method:** {method}")
            if run.get("fix_attempts", 0) > 1:
                lines.append(f"- **Fix attempts:** {run['fix_attempts']}")
            if error:
                lines.append(f"- **Error:** {error[:500]}")
            ai_output = run.get("ai_output", "")
            if ai_output:
                lines.append(f"- **AI output preview:** {ai_output[:300]}")

            # Include rejection feedback for this run (#15).
            feedback_rows = conn.execute(
                "SELECT * FROM feedback WHERE run_id = ? ORDER BY id ASC",
                (run["id"],),
            ).fetchall()
            if feedback_rows:
                lines.append("- **User corrections:**")
                for fb in feedback_rows:
                    fb = dict(fb)
                    lines.append(f"  - `{fb['file_path']}` ({fb['action']})")
                    if fb.get("comment"):
                        lines.append(f"    Comment: {fb['comment'][:200]}")
                    if fb.get("diff"):
                        lines.append(f"    ```diff\n{fb['diff'][:500]}\n    ```")

            lines.append("")
    finally:
        conn.close()

    lines.append(
        "> Use the above history to avoid repeating mistakes. "
        "If a previous approach failed, try a different strategy.\n"
    )
    return "\n".join(lines)


# ── Rejection feedback (#16) ──


def save_feedback(run_id: int, file_path: str, action: str = "edit",
                  diff: str = "", comment: str = "") -> int:
    """Store user correction/rejection feedback for a run."""
    conn = _get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO feedback (run_id, file_path, action, diff, comment, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, file_path, action, diff[:5000], comment[:1000],
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        logger.info("Feedback saved for run %d, file %s.", run_id, file_path)
        return cur.lastrowid
    finally:
        conn.close()


def load_feedback_for_story(work_item_id: int) -> list[dict]:
    """Load all feedback entries across all runs for a story."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT f.* FROM feedback f
               JOIN runs r ON f.run_id = r.id
               WHERE r.work_item_id = ?
               ORDER BY f.id ASC""",
            (work_item_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Summary report (for batch mode #13) ──


def generate_batch_summary(run_ids: list[int] | None = None,
                           since: str | None = None) -> str:
    """Generate a markdown summary of batch run results.

    Args:
        run_ids: Specific run IDs to summarise, or
        since: ISO timestamp — summarise all runs after this time.
    """
    conn = _get_connection()
    try:
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            rows = conn.execute(
                f"SELECT * FROM runs WHERE id IN ({placeholders}) ORDER BY id ASC",
                run_ids,
            ).fetchall()
        elif since:
            rows = conn.execute(
                "SELECT * FROM runs WHERE timestamp >= ? ORDER BY id ASC",
                (since,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 50"
            ).fetchall()

        if not rows:
            return "No runs found."

        runs = [dict(r) for r in rows]
        successes = [r for r in runs if not r.get("failed_stage")]
        failures = [r for r in runs if r.get("failed_stage")]

        lines = [
            "# Batch Run Summary",
            "",
            f"**Total:** {len(runs)} | **Passed:** {len(successes)} | **Failed:** {len(failures)}",
            f"**Success rate:** {len(successes) / len(runs) * 100:.0f}%",
            "",
            "## Results",
            "",
            "| # | Story | Result | Method | Branch | Fix Attempts |",
            "|---|-------|--------|--------|--------|--------------|",
        ]

        for r in runs:
            status = "✅ PASS" if not r.get("failed_stage") else f"❌ {r['failed_stage']}"
            lines.append(
                f"| {r['id']} | #{r['work_item_id']} | {status} | "
                f"{r.get('method', '')} | {r.get('branch', '')} | {r.get('fix_attempts', 0)} |"
            )

        if failures:
            lines.extend(["", "## Failure Details", ""])
            for r in failures:
                lines.append(f"### Story #{r['work_item_id']} — failed at {r['failed_stage']}")
                if r.get("error"):
                    lines.append(f"```\n{r['error'][:500]}\n```")
                lines.append("")

        return "\n".join(lines)
    finally:
        conn.close()


# ── Migration helper ──


def migrate_from_json(config: dict) -> int:
    """One-time migration: import records from old .pipeline-history.json into SQLite."""
    json_path = _DATA_DIR / ".pipeline-history.json"
    if not json_path.exists():
        return 0

    try:
        records = json.loads(json_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    count = 0
    for record in records:
        save_run_record(config, record)
        count += 1

    if count:
        # Rename old file to indicate migration done.
        json_path.rename(json_path.with_suffix(".json.migrated"))
        logger.info("Migrated %d records from JSON to SQLite.", count)

    return count


# ── Token usage tracking ──


def save_token_usage(
    *,
    story_id: int | None,
    stage: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Record token usage for a single API call."""
    total = prompt_tokens + completion_tokens
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO token_usage
               (story_id, stage, provider, model, prompt_tokens, completion_tokens, total_tokens, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                story_id,
                stage,
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                total,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        logger.debug("Token usage: story=%s stage=%s tokens=%d (%d+%d)",
                      story_id, stage, total, prompt_tokens, completion_tokens)
    finally:
        conn.close()


def get_usage_by_story(story_id: int) -> dict:
    """Get token usage summary for a specific story.

    Returns dict with 'total_tokens', 'prompt_tokens', 'completion_tokens',
    'calls' (count), and 'breakdown' (list of per-stage dicts).
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT stage, provider, model, prompt_tokens, completion_tokens,
                      total_tokens, timestamp
               FROM token_usage WHERE story_id = ? ORDER BY timestamp""",
            (story_id,),
        ).fetchall()

        total_prompt = sum(r["prompt_tokens"] for r in rows)
        total_completion = sum(r["completion_tokens"] for r in rows)
        total_total = sum(r["total_tokens"] for r in rows)

        breakdown = [dict(r) for r in rows]

        return {
            "story_id": story_id,
            "total_tokens": total_total,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "calls": len(rows),
            "breakdown": breakdown,
        }
    finally:
        conn.close()


def get_usage_summary(days: int = 7) -> dict:
    """Get aggregate token usage for the last N days.

    Returns dict with daily totals, per-story totals, and grand totals.
    """
    conn = _get_connection()
    try:
        cutoff = datetime.now(timezone.utc).isoformat()[:10]  # today
        # Calculate cutoff date.
        from datetime import timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff = cutoff_dt.isoformat()

        # Per-day totals.
        daily_rows = conn.execute(
            """SELECT DATE(timestamp) as day,
                      SUM(prompt_tokens) as prompt,
                      SUM(completion_tokens) as completion,
                      SUM(total_tokens) as total,
                      COUNT(*) as calls
               FROM token_usage WHERE timestamp >= ?
               GROUP BY DATE(timestamp) ORDER BY day""",
            (cutoff,),
        ).fetchall()

        # Per-story totals.
        story_rows = conn.execute(
            """SELECT story_id,
                      SUM(prompt_tokens) as prompt,
                      SUM(completion_tokens) as completion,
                      SUM(total_tokens) as total,
                      COUNT(*) as calls
               FROM token_usage WHERE timestamp >= ? AND story_id IS NOT NULL
               GROUP BY story_id ORDER BY total DESC""",
            (cutoff,),
        ).fetchall()

        # Per-provider totals.
        provider_rows = conn.execute(
            """SELECT provider, model,
                      SUM(total_tokens) as total,
                      COUNT(*) as calls
               FROM token_usage WHERE timestamp >= ?
               GROUP BY provider, model ORDER BY total DESC""",
            (cutoff,),
        ).fetchall()

        grand_total = sum(r["total"] for r in daily_rows)

        return {
            "days": days,
            "grand_total_tokens": grand_total,
            "daily": [dict(r) for r in daily_rows],
            "by_story": [dict(r) for r in story_rows],
            "by_provider": [dict(r) for r in provider_rows],
        }
    finally:
        conn.close()
