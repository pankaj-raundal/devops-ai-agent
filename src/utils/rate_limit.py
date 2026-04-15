"""Rate limit tracking for GitHub Models API and other providers."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("devops_ai_agent.rate_limit")

# Cooldown file stored in project .dai/ directory.
_COOLDOWN_FILE = Path(__file__).resolve().parent.parent.parent / ".dai" / "rate-limit-cooldown.json"


def check_cooldown(provider: str) -> str | None:
    """Check if a rate limit cooldown is active for the given provider.

    Returns a user-friendly message if the provider is in cooldown, or None
    if the provider is clear to use.
    """
    if not _COOLDOWN_FILE.exists():
        return None

    try:
        data = json.loads(_COOLDOWN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    entry = data.get(provider)
    if not entry:
        return None

    reset_at = entry.get("reset_at", 0)
    now = time.time()
    if now >= reset_at:
        # Cooldown expired — clear it.
        _clear_cooldown(provider)
        return None

    remaining = int(reset_at - now)
    limit_type = entry.get("limit_type", "unknown")
    reset_time = datetime.fromtimestamp(reset_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    hours = remaining // 3600
    mins = (remaining % 3600) // 60

    if hours > 0:
        time_str = f"{hours}h {mins}m"
    else:
        time_str = f"{mins}m"

    return (
        f"⏳ {provider} API daily quota exceeded ({limit_type}).\n"
        f"   Resets at: {reset_time} ({time_str} from now)\n"
        f"   Options:\n"
        f"   1. Wait for the quota to reset\n"
        f"   2. Switch to a different provider (set ANTHROPIC_API_KEY or OPENAI_API_KEY)\n"
        f"   3. Use a paid API plan for higher limits"
    )


def record_rate_limit(provider: str, error: Exception) -> None:
    """Extract rate limit info from an API error and record cooldown.

    Parses retry-after and x-ratelimit-type headers from the response.
    Only records cooldown for daily limits (not per-minute transient limits).
    """
    retry_after = 0
    limit_type = "unknown"

    # Extract headers from OpenAI SDK's RateLimitError.
    response = getattr(error, "response", None)
    if response is not None:
        headers = getattr(response, "headers", {})
        limit_type = headers.get("x-ratelimit-type", "unknown")
        try:
            retry_after = int(headers.get("retry-after", "0"))
        except (ValueError, TypeError):
            retry_after = 0

    # Only record cooldown for daily limits (>= 5 minutes),
    # not transient per-minute throttles.
    if retry_after < 300:
        logger.debug("Rate limit is transient (%ds) — not recording cooldown.", retry_after)
        return

    reset_at = time.time() + retry_after
    reset_time = datetime.fromtimestamp(reset_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logger.warning("Daily quota exceeded for %s (%s). Resets at %s (%ds).",
                    provider, limit_type, reset_time, retry_after)

    _COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        data = json.loads(_COOLDOWN_FILE.read_text()) if _COOLDOWN_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}

    data[provider] = {
        "reset_at": reset_at,
        "retry_after": retry_after,
        "limit_type": limit_type,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    _COOLDOWN_FILE.write_text(json.dumps(data, indent=2))


def _clear_cooldown(provider: str) -> None:
    """Remove a provider's cooldown entry."""
    if not _COOLDOWN_FILE.exists():
        return
    try:
        data = json.loads(_COOLDOWN_FILE.read_text())
        data.pop(provider, None)
        if data:
            _COOLDOWN_FILE.write_text(json.dumps(data, indent=2))
        else:
            _COOLDOWN_FILE.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError):
        pass
