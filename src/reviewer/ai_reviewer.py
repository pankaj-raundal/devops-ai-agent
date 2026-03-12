"""AI-powered code reviewer — sends diffs to an AI model for review."""

from __future__ import annotations

import logging

import anthropic
import openai

logger = logging.getLogger("devops_ai_agent.ai_reviewer")

REVIEW_SYSTEM_PROMPT = """\
You are a senior Drupal developer performing a code review.
Review the provided git diff for:
1. Correctness — does the code do what the story requires?
2. Security — OWASP Top 10, SQL injection, XSS, access control.
3. Drupal standards — PSR-12, Drupal coding conventions, proper DI.
4. PHP 8.4 compatibility.
5. Performance — unnecessary DB queries, N+1 issues, missing caching.
6. Test coverage — are there tests for the changes?

Output a structured review with:
- APPROVE / REQUEST_CHANGES / COMMENT
- A list of findings (file, line, severity, comment)
- An overall summary.
"""


class AIReviewer:
    """Sends diffs to an AI model for automated code review."""

    def __init__(self, config: dict):
        ai = config.get("ai_agent", {})
        self.provider = ai.get("provider", "anthropic")
        self.model = ai.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = ai.get("max_tokens", 4096)
        self.require_consent = ai.get("require_consent", True)

    def review(self, diff: str, story_context: str) -> dict:
        """Review a diff against story context.

        Returns:
            dict with keys: verdict (str), findings (str), summary (str)
        """
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
                full_payload=REVIEW_SYSTEM_PROMPT + "\n\n" + prompt,
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
        msg = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.1,
            system=REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    def _call_openai(self, prompt: str) -> str:
        client = openai.OpenAI()
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.1,
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content

    def _call_copilot(self, prompt: str) -> str:
        """Call GitHub Models API — OpenAI-compatible, uses `gh auth token`."""
        from src.agent.implement import _get_github_token

        token = _get_github_token()
        client = openai.OpenAI(
            base_url="https://models.github.ai/inference/v1",
            api_key=token,
        )
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.1,
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content

    @staticmethod
    def _extract_verdict(text: str) -> str:
        upper = text.upper()
        if "APPROVE" in upper:
            return "APPROVE"
        if "REQUEST_CHANGES" in upper or "REQUEST CHANGES" in upper:
            return "REQUEST_CHANGES"
        return "COMMENT"
