"""CLI entry point — Click-based commands for the DevOps AI Agent."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import load_config

console = Console()


@click.group()
@click.option("--config", "-c", default=None, help="Path to config.local.yaml")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx, config, verbose):
    """DevOps AI Agent — Automated story implementation pipeline."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config(config)
    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg


@main.command()
@click.pass_context
def fetch(ctx):
    """Fetch the latest assigned story from Azure DevOps."""
    from .integrations.azure_devops import AzureDevOpsClient

    client = AzureDevOpsClient(ctx.obj["config"])
    story = client.fetch_latest_story()
    if story:
        console.print(f"[bold green]#{story.id}[/] {story.title}")
        console.print(f"  Type: {story.work_item_type}  State: {story.state}")
        if story.tags:
            console.print(f"  Tags: {story.tags}")
    else:
        console.print("[yellow]No stories found matching criteria.[/]")


@main.command()
@click.option("--story-id", "-s", type=int, default=None, help="Specific work item ID")
@click.option("--skip-tests", is_flag=True, help="Skip test execution")
@click.option("--dry-run", is_flag=True, help="Fetch story and build context only — no branch, AI, or tests")
@click.pass_context
def run(ctx, story_id, skip_tests, dry_run):
    """Run the full pipeline: Fetch → Branch → Implement → Test → Review."""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj["config"])
    results = pipeline.run(work_item_id=story_id, skip_tests=skip_tests, dry_run=dry_run)

    table = Table(title="Pipeline Results")
    table.add_column("Stage", style="cyan")
    table.add_column("Status")
    table.add_column("Details")

    for r in results:
        status = "[green]PASS[/]" if r.success else "[red]FAIL[/]"
        detail = r.details.get("title", "") or r.details.get("branch", "") or r.details.get("method", "") or r.details.get("verdict", "") or r.error
        table.add_row(r.stage.value, status, str(detail)[:80])

    console.print(table)

    if not all(r.success for r in results):
        sys.exit(1)


@main.command()
@click.argument("ticket_id", type=int)
@click.pass_context
def from_zendesk(ctx, ticket_id):
    """Run pipeline starting from a Zendesk ticket ID."""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj["config"])
    results = pipeline.run_from_zendesk(ticket_id)

    for r in results:
        status = "✓" if r.success else "✗"
        console.print(f"  {status} {r.stage.value}: {r.details or r.error}")

    if not all(r.success for r in results):
        sys.exit(1)


@main.command(name="run-all")
@click.option("--skip-tests", is_flag=True, help="Skip test execution")
@click.option("--dry-run", is_flag=True, help="Dry run mode")
@click.pass_context
def run_all(ctx, skip_tests, dry_run):
    """Fetch all matching stories and run the pipeline on each sequentially."""
    from .pipeline import Pipeline

    pipeline = Pipeline(ctx.obj["config"])
    all_results = pipeline.run_queue(skip_tests=skip_tests, dry_run=dry_run)

    if not all_results:
        console.print("[yellow]No stories found in queue.[/]")
        return

    table = Table(title=f"Queue Results — {len(all_results)} Stories")
    table.add_column("Story", style="cyan")
    table.add_column("Status")
    table.add_column("Stages")

    any_failed = False
    for wid, results in all_results.items():
        success = all(r.success for r in results)
        if not success:
            any_failed = True
        status = "[green]PASS[/]" if success else "[red]FAIL[/]"
        stages = ", ".join(r.stage.value for r in results)
        table.add_row(f"#{wid}", status, stages[:80])

    console.print(table)

    if any_failed:
        sys.exit(1)


@main.command()
@click.pass_context
def implement(ctx):
    """Implement the current story (reads .current-story.md)."""
    from .agent.context_builder import load_story_context, _DATA_DIR, CONTEXT_FILENAME
    from .agent.implement import ImplementationAgent

    context_path = _DATA_DIR / CONTEXT_FILENAME

    if not context_path.exists():
        console.print("[red]No .current-story.md found. Run 'dai fetch' first.[/]")
        sys.exit(1)

    story_context = load_story_context(ctx.obj["config"])
    agent = ImplementationAgent(ctx.obj["config"])
    result = agent.implement(story_context)

    if result["success"]:
        console.print(f"[green]Implementation succeeded via {result['method']}[/]")
    else:
        console.print(f"[red]Implementation failed: {result['output'][:200]}[/]")
        sys.exit(1)


@main.command()
@click.pass_context
def review(ctx):
    """Run tests and AI code review on current changes."""
    from .integrations.git_manager import GitManager
    from .reviewer.ai_reviewer import AIReviewer
    from .reviewer.test_runner import TestRunner

    config = ctx.obj["config"]

    # Tests.
    console.print("[bold]Running tests...[/]")
    git = GitManager(config)
    changed_files = git.get_changed_files()
    runner = TestRunner(config)
    summary = runner.run_all(changed_files=changed_files if changed_files else None)
    console.print(summary.summary_text())

    # AI review.
    console.print("\n[bold]Running AI review...[/]")
    diff = git.get_diff()
    if not diff.strip():
        console.print("[yellow]No changes to review.[/]")
        return

    workspace = Path(config["project"]["workspace_dir"])
    from .agent.context_builder import _DATA_DIR, CONTEXT_FILENAME
    context_path = _DATA_DIR / CONTEXT_FILENAME
    story_context = context_path.read_text() if context_path.exists() else ""

    reviewer = AIReviewer(config)
    result = reviewer.review(diff, story_context)
    console.print(f"\nVerdict: [bold]{result['verdict']}[/]")
    console.print(result["summary"])


@main.command()
@click.pass_context
def watch(ctx):
    """Poll Azure DevOps for new stories and auto-run pipeline."""
    from .integrations.azure_devops import AzureDevOpsClient
    from .pipeline import Pipeline

    import time

    config = ctx.obj["config"]
    interval = config.get("webhook", {}).get("poll_interval", 300)
    seen_ids: set[int] = set()

    # Pre-populate seen_ids from local run history (avoid re-processing).
    from .agent.context_builder import load_run_history

    for record in load_run_history(config):
        wid = record.get("work_item_id")
        if wid and record.get("failed_stage") is None:
            seen_ids.add(wid)

    console.print(f"[bold]Watching for new stories (every {interval}s)...[/]")
    if seen_ids:
        console.print(f"[dim]Skipping {len(seen_ids)} previously processed story ID(s): {seen_ids}[/]")

    client = AzureDevOpsClient(config)
    pipeline = Pipeline(config)

    while True:
        stories = client.fetch_all_stories()
        new_stories = [s for s in stories if s.id not in seen_ids]
        if new_stories:
            console.print(f"\n[bold]Found {len(new_stories)} new story/stories in queue.[/]")
            for i, story in enumerate(new_stories, 1):
                seen_ids.add(story.id)
                console.print(f"\n[bold green][{i}/{len(new_stories)}] Story #{story.id}: {story.title}[/]")
                results = pipeline.run(work_item_id=story.id)
                for r in results:
                    status = "✓" if r.success else "✗"
                    console.print(f"  {status} {r.stage.value}")
        time.sleep(interval)


@main.command()
@click.option("--port", "-p", type=int, default=8080, help="Port to listen on")
@click.pass_context
def webhook(ctx, port):
    """Start webhook server for push-based triggers."""
    from .integrations.webhook_server import create_app
    from .pipeline import Pipeline

    config = ctx.obj["config"]
    pipeline = Pipeline(config)

    def on_devops(work_item_id, event_type, payload):
        console.print(f"[cyan]DevOps event: {event_type} for #{work_item_id}[/]")
        pipeline.run(work_item_id=int(work_item_id))

    def on_zendesk(ticket_id, payload):
        console.print(f"[cyan]Zendesk event: ticket #{ticket_id}[/]")
        pipeline.run_from_zendesk(int(ticket_id))

    app = create_app(config, on_devops, on_zendesk)
    console.print(f"[bold]Webhook server listening on port {port}[/]")
    app.run(host="0.0.0.0", port=port)


@main.command()
@click.option("--port", "-p", type=int, default=8090, help="Port for dashboard")
@click.pass_context
def dashboard(ctx, port):
    """Start the web dashboard for pipeline monitoring."""
    from .dashboard.app import create_dashboard

    config = ctx.obj["config"]
    app = create_dashboard(config)
    console.print(f"[bold green]Dashboard running at http://localhost:{port}[/]")
    console.print("Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
