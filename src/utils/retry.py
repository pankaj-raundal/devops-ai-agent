"""Retry utilities — exponential backoff and provider failover for AI API calls."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger("devops_ai_agent.retry")

T = TypeVar("T")

# Exceptions that warrant a retry (transient).
_RETRYABLE_ANTHROPIC = ("anthropic.APIConnectionError", "anthropic.APITimeoutError")
_RETRYABLE_OPENAI = ("openai.APIConnectionError", "openai.APITimeoutError")


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    retryable_exceptions: tuple = (),
    label: str = "operation",
) -> T:
    """Call fn(), retrying on transient errors with exponential backoff.

    Args:
        fn: Zero-argument callable to execute.
        max_retries: Maximum number of retry attempts (total tries = max_retries + 1).
        base_delay: Initial delay in seconds (doubles each attempt).
        max_delay: Cap on delay between retries.
        retryable_exceptions: Exception types to catch and retry on.
        label: Human-readable label for log messages.

    Returns:
        The return value of fn() on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    delay = base_delay

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            wait = min(delay, max_delay)
            logger.warning(
                "%s attempt %d/%d failed (%s: %s) — retrying in %.0fs...",
                label, attempt + 1, max_retries + 1,
                type(exc).__name__, exc, wait,
            )
            time.sleep(wait)
            delay *= 2  # Exponential backoff.
        except Exception:
            # Non-retryable — propagate immediately.
            raise

    raise last_exc  # type: ignore[misc]


def with_anthropic_retry(fn: Callable[[], T], label: str = "Anthropic API call") -> T:
    """Retry an Anthropic API call on transient connection/timeout errors."""
    try:
        import anthropic
        retryable = (anthropic.APIConnectionError, anthropic.APITimeoutError)
    except ImportError:
        retryable = ()

    return retry_with_backoff(fn, max_retries=3, base_delay=3.0, retryable_exceptions=retryable, label=label)


def with_openai_retry(fn: Callable[[], T], label: str = "OpenAI API call") -> T:
    """Retry an OpenAI API call on transient connection/timeout errors."""
    try:
        import openai
        retryable = (openai.APIConnectionError, openai.APITimeoutError)
    except ImportError:
        retryable = ()

    return retry_with_backoff(fn, max_retries=3, base_delay=3.0, retryable_exceptions=retryable, label=label)


class ProviderFailover:
    """Try a primary AI provider, fall back to an alternate on hard failure.

    Usage:
        failover = ProviderFailover(primary="anthropic", fallback="openai")
        result = failover.run(primary_fn, fallback_fn)
    """

    def __init__(self, primary: str, fallback: str):
        self.primary = primary
        self.fallback = fallback

    def run(self, primary_fn: Callable[[], T], fallback_fn: Callable[[], T]) -> T:
        """Try primary_fn first; if it raises, log and try fallback_fn."""
        try:
            logger.debug("ProviderFailover: trying primary (%s).", self.primary)
            return primary_fn()
        except Exception as primary_exc:
            logger.warning(
                "ProviderFailover: primary (%s) failed (%s: %s) — switching to fallback (%s).",
                self.primary, type(primary_exc).__name__, primary_exc, self.fallback,
            )
            try:
                return fallback_fn()
            except Exception as fallback_exc:
                logger.error(
                    "ProviderFailover: fallback (%s) also failed: %s",
                    self.fallback, fallback_exc,
                )
                raise fallback_exc
