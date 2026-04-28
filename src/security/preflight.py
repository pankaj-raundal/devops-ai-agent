"""Phase 2 — Permission preflight.

Detects overprivileged credentials BEFORE the pipeline runs and refuses
to proceed if dangerous scopes are found. The user can override with
`--i-accept-the-risk` (logged loudly).

Checks performed:
  1. ADO auth: `az login` (full personal account) vs scoped PAT
  2. GitHub token scopes (parse `X-OAuth-Scopes` response header)
  3. Cloud credential leaks in env (AWS_*, KUBECONFIG, etc.)
  4. Claude CLI present and reachable

See doc/security-design.md §"Layer 1 — Permission Preflight".
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Literal

from . import FORBIDDEN_ENV_VARS

logger = logging.getLogger("devops_ai_agent.security.preflight")

Level = Literal["INFO", "WARN", "HIGH", "CRITICAL"]


# ---------------------------------------------------------------------------
# Scope policy
# ---------------------------------------------------------------------------
# ADO PAT scope tokens that should NEVER be granted to the agent.
# (Source: doc/security-design.md §1.1)
FORBIDDEN_ADO_SCOPES: set[str] = {
    "vso.project_manage",
    "vso.security_manage",
    "vso.build_execute",
    "vso.release_manage",
    "vso.identity_manage",
    "vso.tokenadministration",
    "vso.profile_write",
}

# GitHub classic-PAT scopes that should be refused.
FORBIDDEN_GITHUB_SCOPES: set[str] = {
    "delete_repo",
    "admin:org",
    "admin:repo_hook",
    "admin:enterprise",
    "admin:gpg_key",
    "admin:ssh_signing_key",
    "site_admin",
}

# Minimum needed for normal pipeline operation (informational).
RECOMMENDED_GITHUB_SCOPES: set[str] = {"repo", "read:user"}


@dataclass
class SecurityFinding:
    """One result from a preflight check."""

    code: str
    level: Level
    message: str
    fix: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        return self.level == "CRITICAL"

    def render(self) -> str:
        icon = {"INFO": "ℹ", "WARN": "⚠", "HIGH": "⚠", "CRITICAL": "✗"}[self.level]
        out = f"{icon} {self.level}: {self.message}"
        if self.fix:
            out += f"\n    → {self.fix}"
        return out


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_ado_token(config: dict | None = None) -> SecurityFinding:
    """Detect whether ADO is being accessed via personal `az login` or a PAT."""
    pat = os.environ.get("AZURE_DEVOPS_PAT") or os.environ.get("AZURE_DEVOPS_EXT_PAT")
    if not pat:
        # Falling back to `az login` — full tenant access of the user.
        return SecurityFinding(
            code="ADO_NO_SCOPED_TOKEN",
            level="HIGH",
            message="No AZURE_DEVOPS_PAT set — agent will inherit your full `az login` access.",
            fix=(
                "Create a scoped PAT at https://dev.azure.com/<org>/_usersSettings/tokens\n"
                "      Required scopes: Work Items (R&W), Code (R&W)\n"
                "      Forbidden scopes: anything 'Manage' or 'Admin'\n"
                "      Then: export AZURE_DEVOPS_PAT=xxx"
            ),
        )

    # PAT is set — we cannot reliably probe scopes from a PAT (Azure DevOps
    # does not expose them in any public endpoint). We still verify the
    # token works against the org's connectionData endpoint.
    org = (config or {}).get("azure_devops", {}).get("organization", "")
    if org:
        ok = _probe_ado_pat(pat, org)
        if not ok:
            return SecurityFinding(
                code="ADO_PAT_INVALID",
                level="CRITICAL",
                message=f"AZURE_DEVOPS_PAT failed validation against {org}.",
                fix="Regenerate the PAT and confirm it has 'Work Items (R&W)' + 'Code (R&W)'.",
            )
    return SecurityFinding(
        code="ADO_PAT_PRESENT",
        level="INFO",
        message="AZURE_DEVOPS_PAT is set (scopes cannot be probed; verify manually).",
        fix="Confirm the PAT only has: Work Items (R&W), Code (R&W). No 'Manage' scopes.",
    )


def _probe_ado_pat(pat: str, organization: str) -> bool:
    """Call connectionData endpoint to verify the PAT works. Returns True on success."""
    import base64
    try:
        import httpx
    except ImportError:
        return True  # don't hard-fail if httpx missing
    org = organization.rstrip("/").rsplit("/", 1)[-1]
    url = f"https://dev.azure.com/{org}/_apis/connectionData?api-version=7.1"
    auth = base64.b64encode(f":{pat}".encode()).decode()
    try:
        r = httpx.get(url, headers={"Authorization": f"Basic {auth}"}, timeout=8.0)
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.debug("ADO PAT probe failed: %s", exc)
        return False


def check_github_token() -> SecurityFinding:
    """Probe GitHub API to read the token's actual scopes."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return SecurityFinding(
            code="GH_NO_TOKEN",
            level="INFO",
            message="No GITHUB_TOKEN set (skip if you are not pushing to GitHub).",
        )

    try:
        import httpx
    except ImportError:
        return SecurityFinding(
            code="GH_PROBE_SKIPPED",
            level="INFO",
            message="httpx not installed — cannot probe GitHub token scopes.",
        )

    try:
        r = httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=8.0,
        )
    except Exception as exc:  # noqa: BLE001
        return SecurityFinding(
            code="GH_PROBE_FAILED",
            level="WARN",
            message=f"GitHub API unreachable: {exc}",
        )

    if r.status_code == 401:
        return SecurityFinding(
            code="GH_TOKEN_INVALID",
            level="CRITICAL",
            message="GITHUB_TOKEN is invalid or revoked.",
            fix="Regenerate at https://github.com/settings/tokens (use a fine-grained PAT scoped to a single repo).",
        )

    # Fine-grained PATs: scopes header is empty; the GH API returns
    # `X-Accepted-GitHub-Permissions` instead. Classic PATs put scopes in `X-OAuth-Scopes`.
    classic_scopes = r.headers.get("x-oauth-scopes", "")
    if classic_scopes == "":
        # Likely a fine-grained PAT (or a repo-only token with no scopes header).
        return SecurityFinding(
            code="GH_FINE_GRAINED",
            level="INFO",
            message="GITHUB_TOKEN appears fine-grained (recommended).",
        )

    scopes = {s.strip() for s in classic_scopes.split(",") if s.strip()}
    excessive = scopes & FORBIDDEN_GITHUB_SCOPES
    if excessive:
        return SecurityFinding(
            code="GH_OVERSCOPED",
            level="CRITICAL",
            message=f"GITHUB_TOKEN has admin scopes: {sorted(excessive)}",
            fix="Replace with a fine-grained PAT scoped to a single repo (no admin scopes).",
            detail={"scopes": sorted(scopes)},
        )
    return SecurityFinding(
        code="GH_CLASSIC_OK",
        level="WARN",
        message=f"GITHUB_TOKEN is a classic PAT with scopes: {sorted(scopes)}",
        fix="Consider switching to a fine-grained PAT scoped to one repo.",
        detail={"scopes": sorted(scopes)},
    )


def check_cloud_creds() -> list[SecurityFinding]:
    """Refuse if cloud-admin credentials are in the parent env."""
    findings: list[SecurityFinding] = []
    leaked = sorted(k for k in os.environ if k in FORBIDDEN_ENV_VARS)
    if leaked:
        findings.append(SecurityFinding(
            code="CLOUD_CREDS_PRESENT",
            level="HIGH",
            message=f"Cloud / infra credentials found in env: {leaked}",
            fix="These are stripped before invoking the model, but consider unsetting them in this shell.",
            detail={"vars": leaked},
        ))
    return findings


def check_claude_cli() -> SecurityFinding:
    """Verify the claude CLI is installed and supports --allowedTools."""
    if not shutil.which("claude"):
        return SecurityFinding(
            code="CLAUDE_CLI_MISSING",
            level="WARN",
            message="`claude` CLI not found on PATH. Will fall back to API providers.",
            fix="Install: npm install -g @anthropic-ai/claude-code",
        )
    try:
        r = subprocess.run(
            ["claude", "--help"], capture_output=True, text=True, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        return SecurityFinding(
            code="CLAUDE_CLI_BROKEN",
            level="WARN",
            message=f"`claude --help` failed: {exc}",
        )
    if "--allowedTools" not in (r.stdout + r.stderr):
        return SecurityFinding(
            code="CLAUDE_CLI_OUTDATED",
            level="HIGH",
            message="Installed `claude` CLI does not support --allowedTools — security hardening will not apply.",
            fix="Update: npm install -g @anthropic-ai/claude-code@latest",
        )
    return SecurityFinding(
        code="CLAUDE_CLI_OK",
        level="INFO",
        message="`claude` CLI present and supports --allowedTools.",
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def run_preflight(config: dict | None = None) -> list[SecurityFinding]:
    """Run all preflight checks. Returns findings sorted by severity."""
    order = {"CRITICAL": 0, "HIGH": 1, "WARN": 2, "INFO": 3}
    findings: list[SecurityFinding] = []
    findings.append(check_ado_token(config))
    findings.append(check_github_token())
    findings.extend(check_cloud_creds())
    findings.append(check_claude_cli())
    return sorted(findings, key=lambda f: order[f.level])


def has_blocking(findings: list[SecurityFinding]) -> bool:
    """True if any finding is CRITICAL (i.e. should refuse to run)."""
    return any(f.is_blocking for f in findings)


def summarize(findings: list[SecurityFinding]) -> dict[str, int]:
    """Count findings by level."""
    counts = {"CRITICAL": 0, "HIGH": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        counts[f.level] += 1
    return counts
