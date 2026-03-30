"""Dashboard — Flask web UI for the DevOps AI Agent pipeline."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from ..config import load_config
from ..pipeline import Pipeline
from ..utils.data_consent import scan_for_secrets
from ..utils.events import PipelineEvent, event_bus

logger = logging.getLogger("devops_ai_agent.dashboard")

# Consent state shared between the pipeline thread and the dashboard.
_consent_state = {
    "pending": False,
    "action": "",
    "provider": "",
    "model": "",
    "data_summary": [],
    "payload_preview": "",
    "secrets_found": [],
    "payload_length": 0,
    "response": None,  # True/False once user responds.
}
_consent_lock = threading.Lock()

# Plan approval state — shared between pipeline thread and dashboard.
_plan_state = {
    "pending": False,
    "plan": None,  # ImplementationPlan object (set by pipeline thread).
    "plan_dict": None,  # Serialized plan for the UI.
    "response": None,  # dict with per-file approvals + overall approved flag.
}
_plan_lock = threading.Lock()

# Push approval state — shared between pipeline thread and dashboard.
_push_state = {
    "pending": False,
    "branch_name": "",
    "response": None,  # True/False once user responds.
}
_push_lock = threading.Lock()


def _dashboard_plan_approval(plan) -> "ImplementationPlan":
    """Dashboard-based plan approval callback.

    Called by the pipeline thread. Blocks until the user responds via the dashboard UI.
    """
    with _plan_lock:
        _plan_state["pending"] = True
        _plan_state["plan"] = plan
        _plan_state["plan_dict"] = plan.to_dict()
        _plan_state["response"] = None

    # Emit SSE event so the dashboard shows the approval modal.
    event_bus.emit(PipelineEvent("implement", "plan_approval_required",
        "Implementation plan requires your approval", {
            "plan": plan.to_dict(),
        }))

    # Block until user responds (poll every 0.5s).
    while True:
        with _plan_lock:
            if _plan_state["response"] is not None:
                break
        time.sleep(0.5)

    # Clean up.
    with _plan_lock:
        _plan_state["pending"] = False
        _plan_state["plan_dict"] = None
        result_plan = _plan_state["plan"]
        _plan_state["plan"] = None

    return result_plan


def _dashboard_push_approval(branch_name: str) -> bool:
    """Dashboard-based push confirmation callback.

    Called by the pipeline thread. Blocks until the user responds via the dashboard UI.
    """
    with _push_lock:
        _push_state["pending"] = True
        _push_state["branch_name"] = branch_name
        _push_state["response"] = None

    # Emit SSE event so the dashboard shows the push confirmation.
    event_bus.emit(PipelineEvent("push", "push_approval_required",
        f"Push branch {branch_name} to origin?", {
            "branch_name": branch_name,
        }))

    # Block until user responds (poll every 0.5s).
    while True:
        with _push_lock:
            if _push_state["response"] is not None:
                break
        time.sleep(0.5)

    with _push_lock:
        _push_state["pending"] = False
        approved = _push_state["response"]
        _push_state["branch_name"] = ""
        _push_state["response"] = None

    return approved


def create_dashboard(config: dict | None = None) -> Flask:
    """Create the Flask dashboard app."""
    if config is None:
        config = load_config()

    app = Flask(__name__, template_folder="templates")
    app.config["SECRET_KEY"] = "devops-ai-agent-dashboard"  # Local-only, not exposed

    @app.route("/")
    def index():
        return render_template("dashboard.html")

    @app.route("/docs")
    def docs():
        """Serve the architecture documentation page."""
        doc_dir = Path(__file__).resolve().parent.parent.parent / "doc"
        return send_from_directory(doc_dir, "index.html")

    @app.route("/api/events")
    def events_stream():
        """SSE endpoint — streams pipeline events to the browser."""
        def generate():
            q = event_bus.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=30)
                        yield f"data: {event.to_json()}\n\n"
                    except Exception:
                        # Send keepalive.
                        yield f": keepalive\n\n"
            finally:
                event_bus.unsubscribe(q)

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/fetch", methods=["POST"])
    def api_fetch():
        """Fetch latest story from Azure DevOps."""
        from ..integrations.azure_devops import AzureDevOpsClient

        client = AzureDevOpsClient(config)
        story = client.fetch_latest_story()
        if story:
            return jsonify({
                "success": True,
                "story": {
                    "id": story.id,
                    "title": story.title,
                    "type": story.work_item_type,
                    "state": story.state,
                    "tags": story.tags,
                    "description": story.description[:1000],
                    "acceptance_criteria": story.acceptance_criteria[:1000],
                    "comments": story.comments[:10],
                    "url": story.url,
                },
            })
        return jsonify({"success": False, "error": "No stories found matching criteria."})

    @app.route("/api/fetch-all", methods=["POST"])
    def api_fetch_all():
        """Fetch all matching stories from Azure DevOps."""
        from ..integrations.azure_devops import AzureDevOpsClient

        client = AzureDevOpsClient(config)
        stories = client.fetch_all_stories()
        return jsonify({
            "success": True,
            "stories": [
                {
                    "id": s.id,
                    "title": s.title,
                    "type": s.work_item_type,
                    "state": s.state,
                    "tags": s.tags,
                }
                for s in stories
            ],
            "total": len(stories),
        })

    @app.route("/api/run", methods=["POST"])
    def api_run():
        """Start the pipeline in a background thread."""
        data = request.get_json(silent=True) or {}
        story_id = data.get("story_id")
        skip_tests = data.get("skip_tests", False)
        dry_run = data.get("dry_run", False)

        def _run_pipeline():
            pipeline = Pipeline(config)
            # Override consent to use dashboard-based consent.
            ai_cfg = config.get("ai_agent", {})
            ai_cfg["require_consent"] = False  # We handle consent in the dashboard.
            pipeline.implementer.require_consent = False
            pipeline.reviewer.require_consent = False
            # Set dashboard plan approval callback.
            pipeline.set_plan_approval_callback(_dashboard_plan_approval)
            pipeline.set_push_approval_callback(_dashboard_push_approval)
            pipeline.run(
                work_item_id=int(story_id) if story_id else None,
                skip_tests=skip_tests,
                dry_run=dry_run,
            )

        thread = threading.Thread(target=_run_pipeline, daemon=True)
        thread.start()
        return jsonify({"success": True, "message": "Pipeline started."})

    @app.route("/api/run-all", methods=["POST"])
    def api_run_all():
        """Fetch all matching stories and run the pipeline on each sequentially."""
        data = request.get_json(silent=True) or {}
        skip_tests = data.get("skip_tests", False)
        dry_run = data.get("dry_run", False)

        def _run_queue():
            pipeline = Pipeline(config)
            ai_cfg = config.get("ai_agent", {})
            ai_cfg["require_consent"] = False
            pipeline.implementer.require_consent = False
            pipeline.reviewer.require_consent = False
            pipeline.set_plan_approval_callback(_dashboard_plan_approval)
            pipeline.set_push_approval_callback(_dashboard_push_approval)
            pipeline.run_queue(skip_tests=skip_tests, dry_run=dry_run)

        thread = threading.Thread(target=_run_queue, daemon=True)
        thread.start()
        return jsonify({"success": True, "message": "Queue processing started."})

    # --- Plan approval endpoints ---

    @app.route("/api/plan/check", methods=["GET"])
    def plan_check():
        """Check if a plan is pending approval."""
        with _plan_lock:
            return jsonify({
                "pending": _plan_state["pending"],
                "plan": _plan_state["plan_dict"],
            })

    @app.route("/api/plan/respond", methods=["POST"])
    def plan_respond():
        """User responds to plan approval request."""
        data = request.get_json(silent=True) or {}
        approved_files = data.get("approved_files", [])  # list of indices
        overall_approved = data.get("approved", False)

        with _plan_lock:
            plan = _plan_state["plan"]
            if plan:
                for i, fc in enumerate(plan.file_changes):
                    fc.approved = i in approved_files
                plan.approved = overall_approved and any(fc.approved for fc in plan.file_changes)
            _plan_state["response"] = {
                "approved": plan.approved if plan else False,
                "approved_files": approved_files,
            }
            _plan_state["pending"] = False
        return jsonify({"success": True, "approved": plan.approved if plan else False})

    @app.route("/api/plan/file-content", methods=["GET"])
    def plan_file_content():
        """Get the full content of a specific file in the pending plan."""
        idx = request.args.get("index", type=int)
        with _plan_lock:
            plan = _plan_state["plan"]
            if plan and idx is not None and 0 <= idx < len(plan.file_changes):
                fc = plan.file_changes[idx]
                return jsonify({
                    "path": fc.path,
                    "action": fc.action,
                    "content": fc.content,
                })
        return jsonify({"error": "File not found"}), 404

    # --- Push approval endpoints ---

    @app.route("/api/push/check", methods=["GET"])
    def push_check():
        """Check if push confirmation is pending."""
        with _push_lock:
            return jsonify({
                "pending": _push_state["pending"],
                "branch_name": _push_state["branch_name"],
            })

    @app.route("/api/push/respond", methods=["POST"])
    def push_respond():
        """User responds to push confirmation."""
        data = request.get_json(silent=True) or {}
        approved = data.get("approved", False)
        with _push_lock:
            _push_state["response"] = approved
            _push_state["pending"] = False
        return jsonify({"success": True, "approved": approved})

    @app.route("/api/consent/check", methods=["GET"])
    def consent_check():
        """Check if consent is pending."""
        with _consent_lock:
            return jsonify(dict(_consent_state))

    @app.route("/api/consent/respond", methods=["POST"])
    def consent_respond():
        """User responds to consent request."""
        data = request.get_json(silent=True) or {}
        approved = data.get("approved", False)
        with _consent_lock:
            _consent_state["response"] = approved
            _consent_state["pending"] = False
        return jsonify({"success": True, "approved": approved})

    @app.route("/api/consent/scan", methods=["POST"])
    def consent_scan():
        """Scan text for secrets before sending to AI."""
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        findings = scan_for_secrets(text)
        # Strip rich markup for web display.
        clean_findings = [f.replace("[bold red]", "").replace("[/]", "") for f in findings]
        return jsonify({"findings": clean_findings, "safe": len(findings) == 0})

    @app.route("/api/history")
    def api_history():
        """Get event history."""
        events = event_bus.get_history()
        return jsonify([json.loads(e.to_json()) for e in events])

    return app
