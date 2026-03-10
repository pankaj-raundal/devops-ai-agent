"""Webhook server — receives push notifications from Azure DevOps and Zendesk."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Callable

logger = logging.getLogger("devops_ai_agent.webhook")


def create_app(config: dict, on_devops_event: Callable, on_zendesk_event: Callable):
    """Create a Flask app with webhook endpoints.

    Args:
        config: Full application config.
        on_devops_event: Callback for Azure DevOps work item events.
        on_zendesk_event: Callback for Zendesk ticket events.

    Returns:
        Flask app instance.
    """
    # Import Flask lazily — it's an optional dependency.
    try:
        from flask import Flask, Request, abort, jsonify, request
    except ImportError:
        raise ImportError(
            "Flask is required for webhook server. "
            "Install it with: pip install 'devops-ai-agent[webhooks]'"
        )

    app = Flask(__name__)
    webhook_config = config.get("webhook", {})
    secret = webhook_config.get("secret", "")

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.route("/webhooks/azure-devops", methods=["POST"])
    def azure_devops_webhook():
        if not _verify_request(request, secret):
            abort(403)

        payload = request.get_json(silent=True)
        if not payload:
            abort(400)

        event_type = payload.get("eventType", "")
        logger.info("Azure DevOps webhook: %s", event_type)

        # Filter to work item created/updated events.
        if event_type in ("workitem.created", "workitem.updated"):
            resource = payload.get("resource", {})
            work_item_id = resource.get("id") or resource.get("workItemId")
            if work_item_id:
                on_devops_event(work_item_id, event_type, payload)
                return jsonify({"accepted": True})

        return jsonify({"ignored": True})

    @app.route("/webhooks/zendesk", methods=["POST"])
    def zendesk_webhook():
        if not _verify_request(request, secret):
            abort(403)

        payload = request.get_json(silent=True)
        if not payload:
            abort(400)

        logger.info("Zendesk webhook received")
        ticket_id = payload.get("ticket_id") or payload.get("id")
        if ticket_id:
            on_zendesk_event(ticket_id, payload)
            return jsonify({"accepted": True})

        return jsonify({"ignored": True})

    return app


def _verify_request(request, secret: str) -> bool:
    """Verify webhook request authenticity via HMAC signature."""
    if not secret:
        return True  # No secret configured — skip verification.

    signature = request.headers.get("X-Hub-Signature-256", "") or \
                request.headers.get("X-Webhook-Signature", "")
    if not signature:
        logger.warning("No signature header in webhook request.")
        return False

    body = request.get_data()
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
