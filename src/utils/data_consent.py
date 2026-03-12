"""Data consent — shows users what data will be sent to AI and gets approval."""

from __future__ import annotations

import re

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

# Patterns that might indicate sensitive data.
_SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"), "Possible password"),
    (re.compile(r"(?i)(api[_-]?key|apikey|secret[_-]?key)\s*[:=]\s*\S+"), "Possible API key"),
    (re.compile(r"(?i)(token|bearer)\s*[:=]\s*\S+"), "Possible token/secret"),
    (re.compile(r"(?i)(private[_-]?key)"), "Possible private key reference"),
    (re.compile(r"(?i)(connection[_-]?string)\s*[:=]\s*\S+"), "Possible connection string"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub Personal Access Token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI API key"),
    (re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"), "Private key block"),
]


def scan_for_secrets(text: str) -> list[str]:
    """Scan text for patterns that look like secrets or credentials."""
    findings = []
    for pattern, label in _SENSITIVE_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            findings.append(f"  [bold red]⚠ {label}[/] ({len(matches)} occurrence(s))")
    return findings


def request_consent(
    *,
    action: str,
    provider: str,
    model: str,
    data_summary: list[tuple[str, str]],
    full_payload: str,
) -> bool:
    """Display data summary, scan for secrets, and ask for user consent.

    Args:
        action: What the AI will do (e.g. "Implement story", "Review code").
        provider: AI provider name.
        model: Model name.
        data_summary: List of (label, description) tuples summarizing data.
        full_payload: The actual text that will be sent (for secret scanning).

    Returns:
        True if user approves, False otherwise.
    """
    console.print()
    console.rule("[bold yellow]🔒 Data Consent Required[/]")
    console.print()

    # Show what's about to happen.
    console.print(f"  [bold]Action:[/]   {action}")
    console.print(f"  [bold]Provider:[/] {provider}")
    console.print(f"  [bold]Model:[/]    {model}")
    console.print(f"  [bold]Payload:[/]  ~{len(full_payload):,} characters")
    console.print()

    # Show data breakdown.
    console.print("[bold]Data that will be sent to the AI model:[/]")
    for label, desc in data_summary:
        console.print(f"  • [cyan]{label}[/] — {desc}")
    console.print()

    # Scan for secrets.
    findings = scan_for_secrets(full_payload)
    if findings:
        warning = Text()
        warning.append("Potential sensitive data detected:\n", style="bold red")
        console.print(Panel(
            "\n".join(findings),
            title="[bold red]⚠ Security Warning[/]",
            border_style="red",
        ))
        console.print()
    else:
        console.print("  [green]✓ No obvious secrets or credentials detected.[/]")
        console.print()

    # Ask for consent.
    console.print("[bold]Proceed?[/] ", end="")
    try:
        answer = console.input("[Y]es / [N]o / [V]iew full payload: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[red]Aborted.[/]")
        return False

    if answer in ("v", "view"):
        console.print()
        console.print(Panel(
            full_payload[:10000] + ("\n... (truncated)" if len(full_payload) > 10000 else ""),
            title="Payload Preview",
            border_style="dim",
        ))
        console.print()
        try:
            answer = console.input("[bold]Proceed after review?[/] [Y]es / [N]o: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]Aborted.[/]")
            return False

    if answer in ("y", "yes", ""):
        console.print("[green]✓ Consent granted — sending to AI.[/]")
        return True

    console.print("[red]✗ Consent denied — skipping AI call.[/]")
    return False
