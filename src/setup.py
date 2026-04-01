"""Setup and diagnostics — `dai init` wizard and `dai doctor` health check."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

console = Console()

FRAMEWORKS = ["drupal", "python", "react", "java", "dotnet", "angular"]
PROVIDERS = ["anthropic", "openai", "copilot"]
ENV_TYPES = ["ddev", "lando", "docker-compose", "native"]


# ── dai init ──


def run_init(config_dir: Path | None = None) -> Path:
    """Interactive setup wizard — creates config.local.yaml.

    Auto-detects workspace, framework, git branch, and env type where possible.
    Returns the path to the generated config file.
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "config"

    local_path = config_dir / "config.local.yaml"
    if local_path.exists():
        if not click.confirm(
            f"config.local.yaml already exists at {local_path}. Overwrite?",
            default=False,
        ):
            console.print("[yellow]Aborted — keeping existing config.[/]")
            return local_path

    console.print("\n[bold cyan]DevOps AI Agent — Setup Wizard[/]\n")

    # --- Project ---
    workspace = _detect_workspace()
    workspace = click.prompt("Workspace directory", default=str(workspace))
    workspace = str(Path(workspace).expanduser().resolve())

    framework = _detect_framework(Path(workspace))
    framework = click.prompt(
        "Framework",
        type=click.Choice(FRAMEWORKS, case_sensitive=False),
        default=framework,
    )

    module_path = click.prompt(
        "Module/source path (relative to workspace)", default=_guess_module_path(framework)
    )
    project_name = click.prompt("Project name", default=Path(workspace).name)
    base_branch = _detect_base_branch(Path(workspace))
    base_branch = click.prompt("Base branch", default=base_branch)

    # --- Azure DevOps ---
    console.print("\n[bold]Azure DevOps[/]")
    az_org = click.prompt("Organization URL", default="https://dev.azure.com/my-org")
    az_project = click.prompt("Project name", default="")
    az_team = click.prompt("Team name (optional)", default="", show_default=False)
    az_assigned = click.prompt("Assigned to (name contains)", default="")

    # --- AI ---
    console.print("\n[bold]AI Provider[/]")
    provider = click.prompt(
        "Provider",
        type=click.Choice(PROVIDERS, case_sensitive=False),
        default="copilot",
    )
    model = _default_model(provider)
    model = click.prompt("Model", default=model)

    # --- Local env ---
    console.print("\n[bold]Local Environment[/]")
    env_type = _detect_env_type(Path(workspace))
    env_type = click.prompt(
        "Environment type",
        type=click.Choice(ENV_TYPES, case_sensitive=False),
        default=env_type,
    )

    # --- Build config dict ---
    cfg: dict = {
        "project": {
            "name": project_name,
            "workspace_dir": workspace,
            "module_path": module_path,
            "framework": framework,
            "base_branch": base_branch,
        },
        "azure_devops": {
            "organization": az_org,
            "project": az_project,
        },
        "ai_agent": {
            "provider": provider,
            "model": model,
            "review_model": model,
        },
        "local_env": {
            "type": env_type,
        },
    }
    if az_team:
        cfg["azure_devops"]["team"] = az_team
    if az_assigned:
        cfg["azure_devops"]["assigned_to"] = az_assigned

    if env_type == "ddev":
        cfg["local_env"]["drush_prefix"] = "ddev drush"

    # --- Write ---
    config_dir.mkdir(parents=True, exist_ok=True)
    local_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))

    console.print(f"\n[bold green]✓ Config saved to {local_path}[/]")
    console.print("[dim]Edit this file to fine-tune settings. Run `dai doctor` to verify.[/]")
    return local_path


# ── dai doctor ──


# Each check returns (name, passed, message).
Check = tuple[str, bool, str]


def run_doctor(config: dict) -> list[Check]:
    """Run environment health checks and return results."""
    checks: list[Check] = []

    checks.append(_check_python())
    checks.append(_check_git())
    checks.append(_check_workspace(config))
    checks.append(_check_module_path(config))
    checks.append(_check_base_branch(config))
    checks.append(_check_azure_cli())
    checks.append(_check_azure_config(config))
    checks.append(_check_ai_provider(config))
    checks.append(_check_env_type(config))
    checks.append(_check_gh_cli())

    return checks


def print_doctor_results(checks: list[Check]) -> bool:
    """Print health check results as a table. Returns True if all passed."""
    table = Table(title="Environment Health Check")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Details")

    for name, passed, msg in checks:
        status = "[green]✓ OK[/]" if passed else "[red]✗ FAIL[/]"
        table.add_row(name, status, msg)

    console.print(table)

    failed = [c for c in checks if not c[1]]
    if failed:
        console.print(f"\n[red]{len(failed)} check(s) failed.[/] Fix the issues above and run `dai doctor` again.")
        return False
    else:
        console.print("\n[bold green]All checks passed! Ready to run `dai fetch` or `dai run`.[/]")
        return True


# ── Detection helpers ──


def _detect_workspace() -> str:
    """Auto-detect workspace from cwd."""
    return str(Path.cwd())


def _detect_framework(workspace: Path) -> str:
    """Guess framework from project files."""
    if (workspace / "composer.json").exists() or (workspace / "web/sites").exists():
        return "drupal"
    if (workspace / "pyproject.toml").exists() or (workspace / "setup.py").exists():
        return "python"
    if (workspace / "package.json").exists():
        pkg = (workspace / "package.json").read_text()
        if '"react"' in pkg:
            return "react"
        if '"@angular/core"' in pkg:
            return "angular"
        return "react"  # default JS/TS
    if (workspace / "pom.xml").exists():
        return "java"
    if any(workspace.glob("*.csproj")) or any(workspace.glob("*.sln")):
        return "dotnet"
    return "drupal"


def _detect_base_branch(workspace: Path) -> str:
    """Detect default branch from git."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            ref = result.stdout.strip()
            return ref.split("/")[-1]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "master"


def _detect_env_type(workspace: Path) -> str:
    """Detect container environment from project files."""
    if (workspace / ".ddev").exists():
        return "ddev"
    if (workspace / ".lando.yml").exists():
        return "lando"
    if (workspace / "docker-compose.yml").exists() or (workspace / "docker-compose.yaml").exists():
        return "docker-compose"
    return "native"


def _guess_module_path(framework: str) -> str:
    """Provide a reasonable default module path per framework."""
    defaults = {
        "drupal": "web/modules/contrib/my_module",
        "python": "src",
        "react": "src",
        "java": "src/main/java",
        "dotnet": "src",
        "angular": "src/app",
    }
    return defaults.get(framework, "src")


def _default_model(provider: str) -> str:
    defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "copilot": "gpt-4o",
    }
    return defaults.get(provider, "gpt-4o")


# ── Individual checks ──


def _check_python() -> Check:
    import sys
    v = sys.version_info
    ok = v >= (3, 10)
    return ("Python ≥ 3.10", ok, f"Python {v.major}.{v.minor}.{v.micro}")


def _check_git() -> Check:
    if not shutil.which("git"):
        return ("Git", False, "git not found in PATH")
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        return ("Git", True, result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ("Git", False, "git --version failed")


def _check_workspace(config: dict) -> Check:
    ws = config.get("project", {}).get("workspace_dir", "")
    if not ws:
        return ("Workspace dir", False, "project.workspace_dir is empty — run `dai init`")
    p = Path(ws)
    if not p.exists():
        return ("Workspace dir", False, f"{ws} does not exist")
    if not (p / ".git").exists():
        return ("Workspace dir", False, f"{ws} is not a git repository")
    return ("Workspace dir", True, ws)


def _check_module_path(config: dict) -> Check:
    ws = config.get("project", {}).get("workspace_dir", "")
    mp = config.get("project", {}).get("module_path", "")
    if not mp:
        return ("Module path", False, "project.module_path is empty — run `dai init`")
    full = Path(ws) / mp if ws else Path(mp)
    if not full.exists():
        return ("Module path", False, f"{full} does not exist")
    return ("Module path", True, mp)


def _check_base_branch(config: dict) -> Check:
    ws = config.get("project", {}).get("workspace_dir", "")
    branch = config.get("project", {}).get("base_branch", "master")
    if not ws or not Path(ws).exists():
        return ("Base branch", False, "Cannot verify — workspace_dir missing")
    try:
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=ws, capture_output=True, text=True, timeout=5,
        )
        exists = bool(result.stdout.strip())
        if exists:
            return ("Base branch", True, f"{branch} exists")
        return ("Base branch", False, f"Branch '{branch}' not found locally. Run `git fetch`?")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ("Base branch", False, "git branch check failed")


def _check_azure_cli() -> Check:
    if not shutil.which("az"):
        return ("Azure CLI", False, "az not found — install: https://aka.ms/installazurecli")
    try:
        result = subprocess.run(
            ["az", "extension", "show", "--name", "azure-devops", "--query", "version", "-o", "tsv"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return ("Azure CLI", True, f"az + azure-devops ext v{result.stdout.strip()}")
        return ("Azure CLI", False, "azure-devops extension missing — run: az extension add --name azure-devops")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ("Azure CLI", False, "az check failed")


def _check_azure_config(config: dict) -> Check:
    az = config.get("azure_devops", {})
    org = az.get("organization", "")
    proj = az.get("project", "")
    if not org:
        return ("Azure DevOps config", False, "azure_devops.organization is empty")
    if not proj:
        return ("Azure DevOps config", False, "azure_devops.project is empty")
    return ("Azure DevOps config", True, f"{org} / {proj}")


def _check_ai_provider(config: dict) -> Check:
    ai = config.get("ai_agent", {})
    provider = ai.get("provider", "anthropic")
    env_keys = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "copilot": "GITHUB_TOKEN",
    }
    key_name = env_keys.get(provider, "")
    if provider == "copilot":
        # Also accept `gh auth token`
        if os.environ.get("GITHUB_TOKEN"):
            return ("AI provider", True, f"{provider} (GITHUB_TOKEN set)")
        if shutil.which("gh"):
            try:
                result = subprocess.run(
                    ["gh", "auth", "token"], capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return ("AI provider", True, f"{provider} (gh auth token OK)")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        return ("AI provider", False, f"{provider} — set GITHUB_TOKEN or run `gh auth login`")

    if key_name and os.environ.get(key_name):
        return ("AI provider", True, f"{provider} ({key_name} set)")
    return ("AI provider", False, f"{provider} — set {key_name} env var")


def _check_env_type(config: dict) -> Check:
    env_type = config.get("local_env", {}).get("type", "native")
    if env_type == "native":
        return ("Environment", True, "native (no container)")
    cmd = env_type  # ddev, lando, docker-compose
    if shutil.which(cmd):
        return ("Environment", True, f"{env_type} (found in PATH)")
    return ("Environment", False, f"{env_type} not found in PATH — install it or set local_env.type to 'native'")


def _check_gh_cli() -> Check:
    if not shutil.which("gh"):
        return ("GitHub CLI (optional)", True, "Not installed — needed only for GitHub PR creation")
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return ("GitHub CLI", True, "gh authenticated")
        return ("GitHub CLI", False, "gh not authenticated — run `gh auth login`")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ("GitHub CLI", False, "gh auth status failed")
