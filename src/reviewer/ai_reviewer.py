"""AI-powered code reviewer — sends diffs to an AI model for review."""

from __future__ import annotations

import logging

import anthropic
import openai

logger = logging.getLogger("devops_ai_agent.ai_reviewer")

REVIEW_SYSTEM_PROMPT = """\
You are a senior {framework_label} developer performing a code review.
Review the provided git diff for:
{review_criteria}

Output a structured review with:
- APPROVE / REQUEST_CHANGES / COMMENT
- A list of findings (file, line, severity, comment)
- An overall summary.
"""


def _build_review_prompt(config: dict) -> str:
    """Build the review system prompt from the active framework profile.

    Loads the review template from file if configured, otherwise uses
    the built-in REVIEW_SYSTEM_PROMPT constant. Substitutes framework
    placeholders in either case.
    """
    from pathlib import Path

    from src.profiles import get_profile

    profile = get_profile(config)

    # Try loading template file first.
    template_path = config.get("ai_agent", {}).get("review_prompt_template", "")
    if template_path:
        full_path = Path(__file__).parent.parent.parent / template_path
        if full_path.exists():
            template = full_path.read_text()
            return template.replace("{coding_standard}", profile["coding_standard"])

    # Fallback: built-in template with profile values.
    return REVIEW_SYSTEM_PROMPT.format(
        framework_label=profile["framework_label"],
        review_criteria=profile["review_criteria"],
    )


class AIReviewer:
    """Sends diffs to an AI model for automated code review."""

    def __init__(self, config: dict):
        ai = config.get("ai_agent", {})
        self.config = config
        self.provider = ai.get("provider", "anthropic")
        self.model = ai.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = ai.get("max_tokens", 4096)
        self.require_consent = ai.get("require_consent", True)
        trust_level = ai.get("trust_level", "cautious")
        if trust_level in ("balanced", "autonomous", "full-auto"):
            self.require_consent = False
        self._system_prompt = _build_review_prompt(config)
        self.story_id: int | None = None  # Set by pipeline before calling review()

    def _record_usage(self, response, stage: str) -> None:
        """Record token usage from an API response."""
        try:
            from src.history import save_token_usage
            usage = getattr(response, "usage", None)
            if not usage:
                return
            prompt = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
            completion = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
            save_token_usage(
                story_id=self.story_id, stage=stage, provider=self.provider,
                model=self.model, prompt_tokens=prompt, completion_tokens=completion,
            )
        except Exception as e:
            logger.debug("Failed to record token usage: %s", e)

    def review(self, diff: str, story_context: str) -> dict:
        """Review a diff against story context.

        Returns:
            dict with keys: verdict (str), findings (str), summary (str)
        """
        # Fail fast if we're in a rate limit cooldown.
        from src.utils.rate_limit import check_cooldown
        cooldown_msg = check_cooldown(self.provider)
        if cooldown_msg:
            return {
                "verdict": "COMMENT",
                "findings": cooldown_msg,
                "summary": "Review skipped — API quota exceeded.",
            }

        if not diff.strip():
            return {
                "verdict": "COMMENT",
                "findings": "No changes to review.",
                "summary": "Empty diff.",
            }

        prompt = (
            f"## Story Context\n\n{story_context}\n\n"
            f"## Git Diff\n\n```diff\n{diff[:15000]}\n```\n\n"
            f"Review the above changes against the story requirements."
        )

        if self.require_consent:
            from src.utils.data_consent import request_consent

            approved = request_consent(
                action="AI code review",
                provider=self.provider,
                model=self.model,
                data_summary=[
                    ("Story context", "Work item title, description, acceptance criteria"),
                    ("Git diff", f"Code changes (~{len(diff):,} chars, truncated to 15k)"),
                    ("System prompt", "Code review instructions"),
                ],
                full_payload=self._system_prompt + "\n\n" + prompt,
            )
            if not approved:
                return {
                    "verdict": "COMMENT",
                    "findings": "User denied consent to send data.",
                    "summary": "Review skipped — user did not approve data transfer.",
                }

        try:
            if self.provider == "anthropic":
                text = self._call_anthropic(prompt)
            elif self.provider == "copilot":
                text = self._call_copilot(prompt)
            else:
                text = self._call_openai(prompt)

            verdict = self._extract_verdict(text)
            return {"verdict": verdict, "findings": text, "summary": text[:500]}

        except Exception as e:
            logger.error("AI review failed: %s", e)
            return {
                "verdict": "ERROR",
                "findings": str(e),
                "summary": f"Review failed: {e}",
            }

    def _call_anthropic(self, prompt: str) -> str:
        client = anthropic.Anthropic()
        try:
            msg = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.1,
                system=self._system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError as e:
            from ..utils.rate_limit import record_rate_limit
            record_rate_limit("anthropic", e)
            raise
        self._record_usage(msg, "review")
        return msg.content[0].text

    def _call_openai(self, prompt: str) -> str:
        client = openai.OpenAI()
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.1,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        self._record_usage(resp, "review")
        return resp.choices[0].message.content

    def _call_copilot(self, prompt: str) -> str:
        """Call GitHub Models API — OpenAI-compatible, uses `gh auth token`.

        Retries up to 3 times with exponential backoff for transient errors.
        """
        import time

        from src.agent.implement import _get_github_token

        token = _get_github_token()
        client = openai.OpenAI(
            base_url="https://models.github.ai/inference/v1",
            api_key=token,
        )

        last_error = None
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                )
                self._record_usage(resp, "review")
                return resp.choices[0].message.content
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as e:
                last_error = e
                wait = [5, 20, 60][attempt]
                logger.warning("Copilot review API attempt %d failed (%s), retrying in %ds...", attempt + 1, type(e).__name__, wait)
                time.sleep(wait)
            except openai.AuthenticationError:
                raise RuntimeError(
                    "GitHub token rejected by Models API. Run `gh auth login` or check GITHUB_TOKEN."
                )

        if isinstance(last_error, openai.RateLimitError):
            from src.utils.rate_limit import record_rate_limit
            record_rate_limit("copilot", last_error)
            raise RuntimeError(
                "⏳ GitHub Models API rate limit reached during review. "
                "Review will be skipped. Wait a few minutes or use a paid API key."
            )
        raise RuntimeError(f"Copilot review API failed after 3 attempts: {last_error}")

    @staticmethod
    def _extract_verdict(text: str) -> str:
        upper = text.upper()
        if "APPROVE" in upper:
            return "APPROVE"
        if "REQUEST_CHANGES" in upper or "REQUEST CHANGES" in upper:
            return "REQUEST_CHANGES"
        return "COMMENT"
