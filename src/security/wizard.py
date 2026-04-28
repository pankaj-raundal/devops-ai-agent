"""Phase 2 — `dai security setup` interactive wizard.

Walks the user through creating scoped credentials (ADO PAT, GitHub
fine-grained PAT) and writes them to a 0600-mode secrets file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import click
from rich.console import Console

from .preflight import (
    check_ado_token,
    check_github_token,
    check_cloud_creds,
    check_claude_cli,
    has_blocking,
)

console = Console()

ADO_PAT_URL_TMPL = "https://dev.azure.com/{org}/_usersSettings/tokens"
GH_FG_PAT_URL = "https://github.com/settings/personal-access-tokens/new"
SECRETS_FILE = Path.home() / ".config" / "dai" / "credentials.env"


def run_security_setup(config: dict) -> int:
    """Interactive wizard. Returns exit code (0 = success)."""
    console.print("\n[bold cyan]DevOps AI Agent — Security Setup[/]\n")
    console.print(
        "This wizard helps you create [bold]scoped[/] credentials so the AI agent\n"
        "runs with the [bold]minimum[/] permissions needed — not your full personal access.\n"
    )

    org_url = config.get("azure_devops", {}).get("organization", "")
    org_name = org_url.rstrip("/").rsplit("/", 1)[-1] if org_url else "<your-org>"

    # ── ADO ─────────────────────────────────────────────────
    console.print("[bold][1/4] Azure DevOps PAT[/]")
    console.print(f"  Open: {ADO_PAT_URL_TMPL.format(org=org_name)}")
    console.print("  Required scopes: [green]Work Items (Read & Write)[/], [green]Code (Read & Write)[/]")
    console.print("  Forbidden scopes: anything 'Manage' or 'Admin'\n")
    if click.confirm("  Have you created the PAT?", default=True):
        pat = click.prompt("  Paste PAT (input hidden)", hide_input=True, default="", show_default=False)
        if pat:
            os.environ["AZURE_DEVOPS_PAT"] = pat
            ado_finding = check_ado_token(config)
            console.print(f"  → {ado_finding.render()}\n")
        else:
            console.print("  [yellow]Skipped.[/]\n")
            pat = ""
    else:
        console.print("  [yellow]Skipped — you will run with `az login` (high risk).[/]\n")
        pat = ""

    # ── GitHub ──────────────────────────────────────────────
    console.print("[bold][2/4] GitHub fine-grained PAT[/] (optional)")
    console.print(f"  Open: {GH_FG_PAT_URL}")
    console.print("  Repository access: [green]Only select repositories[/] → pick the one repo")
    console.print("  Permissions: contents (R&W), pull requests (R&W), metadata (R)\n")
    gh_token = ""
    if click.confirm("  Configure GitHub token?", default=False):
        gh_token = click.prompt("  Paste token (input hidden)", hide_input=True, default="", show_default=False)
        if gh_token:
            os.environ["GITHUB_TOKEN"] = gh_token
            gh_finding = check_github_token()
            console.print(f"  → {gh_finding.render()}\n")

    # ── Cloud creds ─────────────────────────────────────────
    console.print("[bold][3/4] Cloud credential check[/]")
    cloud_findings = check_cloud_creds()
    if cloud_findings:
        for f in cloud_findings:
            console.print(f"  → {f.render()}")
        console.print("  [dim]These will be stripped before invoking the model.[/]\n")
    else:
        console.print("  [green]✓ No cloud-admin credentials in environment.[/]\n")

    # ── Claude CLI ──────────────────────────────────────────
    console.print("[bold][4/4] Claude CLI[/]")
    cli_finding = check_claude_cli()
    console.print(f"  → {cli_finding.render()}\n")

    # ── Persist secrets ─────────────────────────────────────
    if pat or gh_token:
        if click.confirm(f"\nSave tokens to {SECRETS_FILE}? (mode 0600)", default=True):
            _write_secrets({"AZURE_DEVOPS_PAT": pat, "GITHUB_TOKEN": gh_token})
            console.print(f"[green]✓ Saved.[/] Source it from your shell rc:\n"
                          f"  [dim]set -a; . {SECRETS_FILE}; set +a[/]\n")

    # ── Final preflight ─────────────────────────────────────
    findings = [check_ado_token(config), check_github_token(), *check_cloud_creds(), check_claude_cli()]
    if has_blocking(findings):
        console.print("\n[red bold]✗ Setup incomplete — blocking findings remain.[/]")
        return 2
    console.print("\n[bold green]✓ Security setup complete.[/]")
    console.print("[dim]Run `dai doctor --security` any time to re-check.[/]")
    return 0


def _write_secrets(values: dict[str, str]) -> None:
    """Write tokens to a 0600-mode env file."""
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'export {k}="{v}"' for k, v in values.items() if v]
    SECRETS_FILE.write_text("\n".join(lines) + "\n")
    os.chmod(SECRETS_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
