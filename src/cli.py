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
@click.option("--skip-analysis", is_flag=True, help="Skip AI analysis stage (saves API quota)")
@click.option("--dry-run", is_flag=True, help="Fetch story and build context only — no branch, AI, or tests")
@click.option("--fresh", is_flag=True, help="Discard previous branch and start clean")
@click.option("--ci", is_flag=True, help="CI mode: suppress all interactive prompts, auto-approve plan and push")
@click.option("--skip-git-add", is_flag=True, help="Skip git add/commit/push — AI writes files but does not stage or commit them. You can review and commit manually.")
@click.pass_context
def run(ctx, story_id, skip_tests, skip_analysis, dry_run, fresh, ci, skip_git_add):
    """Run the full pipeline: Fetch → Branch → Implement → Test → Review."""
    from .pipeline import Pipeline
    from .utils.progress import PipelineProgress

    pipeline = Pipeline(ctx.obj["config"], ci_mode=ci)

    with PipelineProgress(console=console) as progress:
        results = pipeline.run(
            work_item_id=story_id,
            skip_tests=skip_tests,
            skip_analysis=skip_analysis,
            dry_run=dry_run,
            fresh=fresh,
            skip_git_add=skip_git_add,
        )

    # Print the final summary table.
    console.print()
    console.print(progress.final_table())

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
@click.option("--trust", type=click.Choice(["cautious", "balanced", "autonomous", "full-auto"]),
              default=None, help="Override trust level for this batch run")
@click.pass_context
def run_all(ctx, skip_tests, dry_run, trust):
    """Fetch all matching stories and run the pipeline on each sequentially.

    For overnight unattended runs, use: dai run-all --trust full-auto
    """
    from .pipeline import Pipeline
    from .history import generate_batch_summary, load_run_history

    config = ctx.obj["config"]
    if trust:
        config.setdefault("ai_agent", {})["trust_level"] = trust
        console.print(f"[dim]Trust level overridden to: {trust}[/]")

    batch_start = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()

    pipeline = Pipeline(config)
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

    # Print batch summary report.
    summary = generate_batch_summary(since=batch_start)
    console.print(f"\n{summary}")

    if any_failed:
        sys.exit(1)


@main.command()
@click.option("--story-id", "-s", type=int, default=None, help="Filter by work item ID")
@click.option("--limit", "-n", type=int, default=20, help="Number of records to show")
@click.pass_context
def history(ctx, story_id, limit):
    """Show pipeline run history from SQLite database."""
    from .history import load_run_history, load_runs_for_story

    if story_id:
        records = load_runs_for_story(story_id)
    else:
        records = load_run_history(ctx.obj["config"], limit=limit)

    if not records:
        console.print("[yellow]No run history found.[/]")
        return

    table = Table(title=f"Run History ({len(records)} records)")
    table.add_column("ID", style="dim")
    table.add_column("Story", style="cyan")
    table.add_column("Result")
    table.add_column("Method")
    table.add_column("Branch")
    table.add_column("Fixes", justify="right")
    table.add_column("Timestamp", style="dim")

    for r in records:
        status = "[green]PASS[/]" if not r.get("failed_stage") else f"[red]{r['failed_stage']}[/]"
        table.add_row(
            str(r.get("id", "")),
            f"#{r.get('work_item_id', '')}",
            status,
            r.get("method", ""),
            r.get("branch", "")[:40],
            str(r.get("fix_attempts", 0)),
            r.get("timestamp", "")[:19],
        )

    console.print(table)


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
    from .history import load_run_history

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


@main.command(name="init")
def init_cmd():
    """Interactive setup wizard — creates config.local.yaml."""
    from .setup import run_init

    run_init()


@main.command()
@click.pass_context
def doctor(ctx):
    """Check environment health — verifies config, tools, and auth."""
    from .setup import run_doctor, print_doctor_results

    config = ctx.obj["config"]
    checks = run_doctor(config)
    all_ok = print_doctor_results(checks)
    if not all_ok:
        sys.exit(1)


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


@main.command()
@click.option("--story-id", "-s", type=int, default=None, help="Show usage for a specific story")
@click.option("--days", "-d", type=int, default=7, help="Number of days to summarize (default: 7)")
@click.pass_context
def usage(ctx, story_id, days):
    """Show token usage statistics."""
    from .history import get_usage_by_story, get_usage_summary

    if story_id:
        data = get_usage_by_story(story_id)
        if not data["calls"]:
            console.print(f"[yellow]No token usage recorded for story #{story_id}.[/]")
            return

        console.print(f"[bold]Token Usage — Story #{story_id}[/]")
        console.print(f"  Total: {data['total_tokens']:,} tokens ({data['prompt_tokens']:,} prompt + {data['completion_tokens']:,} completion)")
        console.print(f"  API calls: {data['calls']}\n")

        table = Table(title="Breakdown")
        table.add_column("Stage", style="cyan")
        table.add_column("Provider")
        table.add_column("Model")
        table.add_column("Prompt", justify="right")
        table.add_column("Completion", justify="right")
        table.add_column("Total", justify="right")
        table.add_column("Time", style="dim")

        for r in data["breakdown"]:
            table.add_row(
                r["stage"], r["provider"], r["model"],
                f"{r['prompt_tokens']:,}", f"{r['completion_tokens']:,}",
                f"{r['total_tokens']:,}", r["timestamp"][:19],
            )
        console.print(table)
    else:
        data = get_usage_summary(days=days)
        if not data["daily"]:
            console.print(f"[yellow]No token usage recorded in the last {days} day(s).[/]")
            return

        console.print(f"[bold]Token Usage Summary — Last {days} Day(s)[/]")
        console.print(f"  Grand total: {data['grand_total_tokens']:,} tokens\n")

        # Daily table.
        daily_table = Table(title="Daily Totals")
        daily_table.add_column("Date", style="cyan")
        daily_table.add_column("Prompt", justify="right")
        daily_table.add_column("Completion", justify="right")
        daily_table.add_column("Total", justify="right")
        daily_table.add_column("Calls", justify="right")

        for r in data["daily"]:
            daily_table.add_row(
                r["day"], f"{r['prompt']:,}", f"{r['completion']:,}",
                f"{r['total']:,}", str(r["calls"]),
            )
        console.print(daily_table)

        # Per-story table.
        if data["by_story"]:
            story_table = Table(title="\nBy Story")
            story_table.add_column("Story", style="cyan")
            story_table.add_column("Total Tokens", justify="right")
            story_table.add_column("Calls", justify="right")

            for r in data["by_story"]:
                story_table.add_row(
                    f"#{r['story_id']}", f"{r['total']:,}", str(r["calls"]),
                )
            console.print(story_table)

        # Per-provider table.
        if data["by_provider"]:
            prov_table = Table(title="\nBy Provider")
            prov_table.add_column("Provider", style="cyan")
            prov_table.add_column("Model")
            prov_table.add_column("Total Tokens", justify="right")
            prov_table.add_column("Calls", justify="right")

            for r in data["by_provider"]:
                prov_table.add_row(
                    r["provider"], r["model"], f"{r['total']:,}", str(r["calls"]),
                )
            console.print(prov_table)


if __name__ == "__main__":
    main()
