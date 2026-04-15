"""Rich live progress display — shows pipeline stages as a live-updating panel."""

from __future__ import annotations

import queue
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .events import PipelineEvent, event_bus

# Map stage names to display labels and order.
STAGE_ORDER = [
    ("fetch_story",    "Fetch Story"),
    ("build_context",  "Build Context"),
    ("analyze",        "AI Analysis"),
    ("create_branch",  "Create Branch"),
    ("implement",      "AI Implementation"),
    ("commit",         "Commit Changes"),
    ("test",           "Run Tests"),
    ("review",         "AI Code Review"),
    ("push",           "Push to Remote"),
    ("pr",             "Create PR"),
    ("complete",       "Complete"),
]

# Status → (icon, style)
STATUS_DISPLAY = {
    "pending":          ("   ", "dim"),
    "running":          (" ~ ", "yellow bold"),
    "pass":             (" + ", "green"),
    "fail":             (" X ", "red bold"),
    "skipped":          (" - ", "dim"),
    "warning":          (" ! ", "yellow"),
    "plan_ready":       (" ? ", "cyan bold"),
    "consent_required": (" ? ", "cyan"),
}


class PipelineProgress:
    """Subscribe to the event bus and render a live Rich progress panel.

    Usage:
        with PipelineProgress() as progress:
            pipeline.run(...)
        # progress.final_table() returns a Rich Table for the summary.
    """

    def __init__(self, console: Console | None = None):
        self._console = console or Console()
        self._stages: dict[str, PipelineEvent] = {}
        self._sub: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._live: Live | None = None
        self._story_title: str = ""
        self._story_id: int | str = ""

    def __enter__(self):
        self._sub = event_bus.subscribe()
        self._stop.clear()
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            transient=True,  # Clears on exit so final_table() can print cleanly.
        )
        self._live.__enter__()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._live:
            self._live.__exit__(*args)
        if self._sub:
            event_bus.unsubscribe(self._sub)

    def _poll_loop(self):
        """Background thread: read events and update state."""
        while not self._stop.is_set():
            try:
                event = self._sub.get(timeout=0.25)
            except queue.Empty:
                continue
            self._process_event(event)
            if self._live:
                self._live.update(self._render())

    def _process_event(self, event: PipelineEvent):
        """Track the latest event per stage."""
        # Capture story info from fetch_story.
        if event.stage == "fetch_story" and event.status == "pass":
            self._story_id = event.details.get("id", "")
            self._story_title = event.details.get("title", event.title)

        # Alert events don't map to a stage row.
        if event.stage == "alert":
            return

        self._stages[event.stage] = event

    def _render(self) -> Panel:
        """Build the Rich panel showing all stages."""
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Icon", width=3, no_wrap=True)
        table.add_column("Stage", min_width=18)
        table.add_column("Status", min_width=50)

        for stage_key, stage_label in STAGE_ORDER:
            event = self._stages.get(stage_key)
            if event is None:
                icon, style = STATUS_DISPLAY["pending"]
                table.add_row(
                    Text(icon, style="dim"),
                    Text(stage_label, style="dim"),
                    Text("", style="dim"),
                )
            else:
                icon, style = STATUS_DISPLAY.get(event.status, ("?", "white"))
                detail = event.title[:65] if event.title else ""
                table.add_row(
                    Text(icon, style=style),
                    Text(stage_label, style=style),
                    Text(detail, style=style if event.status != "pass" else ""),
                )

        header = "DevOps AI Agent"
        if self._story_id:
            header = f"#{self._story_id}  {self._story_title}"

        return Panel(table, title=f"[bold]{header}[/]", border_style="blue", width=90)

    def final_table(self) -> Table:
        """Build a summary table suitable for printing after the live display ends."""
        table = Table(title="Pipeline Results", show_lines=False)
        table.add_column("", width=3, no_wrap=True)
        table.add_column("Stage", style="cyan", min_width=18)
        table.add_column("Result", min_width=8)
        table.add_column("Details", min_width=45)

        for stage_key, stage_label in STAGE_ORDER:
            event = self._stages.get(stage_key)
            if event is None:
                continue
            icon, style = STATUS_DISPLAY.get(event.status, ("?", "white"))
            result = event.status.upper()
            result_style = "green" if event.status == "pass" else "red" if event.status == "fail" else "yellow"
            table.add_row(
                Text(icon, style=style),
                stage_label,
                Text(result, style=result_style),
                event.title[:65] if event.title else "",
            )

        return table
