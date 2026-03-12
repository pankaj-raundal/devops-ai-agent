"""Zendesk integration — poll for assigned tickets, extract details."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger("devops_ai_agent.zendesk")


@dataclass
class ZendeskTicket:
    """Represents a Zendesk ticket."""

    id: int
    subject: str
    description: str
    status: str
    priority: str = ""
    tags: list[str] = field(default_factory=list)
    comments: list[dict[str, str]] = field(default_factory=list)
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    requester_name: str = ""
    requester_email: str = ""


class ZendeskClient:
    """Client for Zendesk REST API."""

    def __init__(self, config: dict):
        zd = config.get("zendesk", {})
        self.enabled = zd.get("enabled", False)
        self.subdomain = zd.get("subdomain", "")
        self.email = zd.get("email", "")
        self.api_token = zd.get("api_token", "")
        self.assignee_name = zd.get("assignee_name", "")
        self.poll_interval = zd.get("poll_interval_seconds", 300)
        self._base_url = f"https://{self.subdomain}.zendesk.com/api/v2"
        self._seen_tickets: set[int] = set()
        self._state_file = Path("logs/.zendesk_last_poll")

    def _auth(self) -> tuple[str, str]:
        """Return basic auth credentials."""
        return (f"{self.email}/token", self.api_token)

    def fetch_assigned_tickets(self, status: str = "open") -> list[ZendeskTicket]:
        """Fetch tickets assigned to the configured user."""
        if not self.enabled:
            logger.debug("Zendesk integration disabled.")
            return []

        query = f'assignee:"{self.assignee_name}" status:{status}'
        logger.info("Searching Zendesk: %s", query)

        try:
            resp = httpx.get(
                f"{self._base_url}/search.json",
                params={"query": query, "sort_by": "created_at", "sort_order": "desc"},
                auth=self._auth(),
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Zendesk API error: %s", e)
            return []

        data = resp.json()
        tickets = []
        for result in data.get("results", []):
            if result.get("result_type") != "ticket":
                continue
            ticket = ZendeskTicket(
                id=result["id"],
                subject=result.get("subject", ""),
                description=result.get("description", ""),
                status=result.get("status", ""),
                priority=result.get("priority", ""),
                tags=result.get("tags", []),
                url=f"https://{self.subdomain}.zendesk.com/agent/tickets/{result['id']}",
                created_at=result.get("created_at", ""),
                updated_at=result.get("updated_at", ""),
            )
            tickets.append(ticket)

        logger.info("Found %d assigned ticket(s).", len(tickets))
        return tickets

    def get_ticket_comments(self, ticket_id: int) -> list[dict[str, str]]:
        """Fetch all comments on a ticket."""
        try:
            resp = httpx.get(
                f"{self._base_url}/tickets/{ticket_id}/comments.json",
                auth=self._auth(),
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Failed to get comments for ticket #%s: %s", ticket_id, e)
            return []

        comments = []
        for c in resp.json().get("comments", []):
            comments.append({
                "author": c.get("author_id", ""),
                "body": c.get("plain_body", c.get("body", "")),
                "created_at": c.get("created_at", ""),
                "public": c.get("public", True),
            })
        return comments

    def get_full_ticket(self, ticket_id: int) -> ZendeskTicket:
        """Fetch a ticket with all its comments."""
        resp = httpx.get(
            f"{self._base_url}/tickets/{ticket_id}.json",
            auth=self._auth(),
            timeout=30,
        )
        resp.raise_for_status()
        t = resp.json()["ticket"]

        # Get requester info.
        requester_name = ""
        requester_email = ""
        requester_id = t.get("requester_id")
        if requester_id:
            try:
                user_resp = httpx.get(
                    f"{self._base_url}/users/{requester_id}.json",
                    auth=self._auth(),
                    timeout=15,
                )
                user_resp.raise_for_status()
                user = user_resp.json().get("user", {})
                requester_name = user.get("name", "")
                requester_email = user.get("email", "")
            except httpx.HTTPError:
                pass

        ticket = ZendeskTicket(
            id=t["id"],
            subject=t.get("subject", ""),
            description=t.get("description", ""),
            status=t.get("status", ""),
            priority=t.get("priority", ""),
            tags=t.get("tags", []),
            url=f"https://{self.subdomain}.zendesk.com/agent/tickets/{t['id']}",
            created_at=t.get("created_at", ""),
            updated_at=t.get("updated_at", ""),
            requester_name=requester_name,
            requester_email=requester_email,
        )
        ticket.comments = self.get_ticket_comments(ticket_id)
        return ticket

    def add_comment(self, ticket_id: int, body: str, public: bool = False) -> bool:
        """Add a comment to a Zendesk ticket.

        Args:
            ticket_id: The ticket to comment on.
            body: Comment text (plain text or HTML).
            public: If True, visible to the requester. Default is internal note.
        """
        if not self.enabled:
            logger.debug("Zendesk integration disabled — skipping comment.")
            return False

        logger.info("Adding %s comment to Zendesk ticket #%s...",
                     "public" if public else "internal", ticket_id)
        try:
            resp = httpx.put(
                f"{self._base_url}/tickets/{ticket_id}.json",
                auth=self._auth(),
                json={
                    "ticket": {
                        "comment": {
                            "body": body,
                            "public": public,
                        }
                    }
                },
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Comment added to Zendesk #%s.", ticket_id)
            return True
        except httpx.HTTPError as e:
            logger.error("Failed to add comment to Zendesk #%s: %s", ticket_id, e)
            return False

    def poll_new_tickets(self) -> list[ZendeskTicket]:
        """Return only tickets not previously seen."""
        tickets = self.fetch_assigned_tickets()
        new_tickets = [t for t in tickets if t.id not in self._seen_tickets]
        self._seen_tickets.update(t.id for t in tickets)
        return new_tickets

    def start_polling(self, callback):
        """Continuously poll for new tickets and call callback for each."""
        logger.info("Starting Zendesk polling (interval: %ds)...", self.poll_interval)
        # Initial load — mark existing tickets as seen.
        self.fetch_assigned_tickets()
        self._seen_tickets.update(t.id for t in self.fetch_assigned_tickets())

        while True:
            try:
                new_tickets = self.poll_new_tickets()
                for ticket in new_tickets:
                    full_ticket = self.get_full_ticket(ticket.id)
                    logger.info("New ticket: #%s — %s", ticket.id, ticket.subject)
                    callback(full_ticket)
            except Exception:
                logger.exception("Error during Zendesk poll cycle")
            time.sleep(self.poll_interval)
