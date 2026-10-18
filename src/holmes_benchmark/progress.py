"""Live terminal status and plain logs, driven by actual request events."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .config import Config
from .report import redact, terminal_text
from .runner import ProgressUpdate


@dataclass
class RequestStatus:
    question: str
    started: float
    last_update: float
    phase: str = "Waiting to send"
    tools: dict[str, str] = field(default_factory=dict)
    outcome: str | None = None
    elapsed: float = 0
    spinner: Spinner = field(default_factory=lambda: Spinner("dots", style="cyan"))

    @property
    def status(self) -> str:
        if self.outcome is not None:
            return self.outcome
        return "Running: " + ", ".join(self.tools.values()) if self.tools else self.phase


class ProgressDisplay:
    def __init__(self, config: Config, *, console: Console | None = None, heartbeat_seconds: float = 5):
        self.config = config
        self.console = console or Console(stderr=True)
        self.heartbeat_seconds = heartbeat_seconds
        self.started = self.last_heartbeat = perf_counter()
        self.rows = {i + 1: RequestStatus(self.safe(config.questions[i % len(config.questions)]),
                                        self.started, self.started) for i in range(config.parallel)}
        self.pending: deque[str] = deque()
        self.live: Live | None = None
        self.output_available = True

    def safe(self, value) -> str:
        return terminal_text(redact(str(value), self.config.token), 100)

    def update(self, event: ProgressUpdate) -> None:
        """Only update memory here; no terminal I/O in the request callback."""
        row = self.rows[event.request_id]
        previous = row.status
        now = perf_counter()
        row.last_update = now
        message = None
        if event.kind == "started":
            row.started = now
            row.phase = "Waiting for response"
        elif event.kind == "connected":
            row.phase = "Waiting for first event"
        elif event.kind == "ai_message":
            row.phase = "Receiving assistant text"
        elif event.kind == "token_count":
            row.phase = "Model working"
        elif event.kind == "conversation_history_compaction_start":
            row.phase = "Compacting context"
        elif event.kind == "conversation_history_compacted":
            row.phase = "Continuing investigation"
        elif event.kind == "start_tool_calling":
            name = self.safe(event.tool_name or "unnamed tool")
            row.tools[event.tool_id or name] = name
            message = "Tool started: " + name
        elif event.kind == "tool_calling_result":
            name = row.tools.pop(event.tool_id or self.safe(event.tool_name or "unnamed tool"), None)
            name = name or self.safe(event.tool_name or "unnamed tool")
            status = self.safe(event.tool_status or "completed")
            row.phase = "Waiting for next event"
            message = f"Tool finished: {name} ({status})"
        elif event.kind == "finished":
            row.outcome = event.outcome or "incomplete"
            row.elapsed = event.elapsed_seconds
            row.tools.clear()
            message = f"{row.outcome} in {row.elapsed:.2f}s"
        if message is not None or row.status != previous:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.pending.append(f"[{stamp}] #{event.request_id}  {message or row.status}")

    def render(self):
        now = perf_counter()
        finished = sum(row.outcome is not None for row in self.rows.values())
        heading = Table.grid(padding=(0, 1))
        heading.add_row(Text("Benchmark", style="bold cyan"),
                        ProgressBar(total=len(self.rows), completed=finished, width=24),
                        Text(f"{finished}/{len(self.rows)} finished  •  {now - self.started:.1f}s elapsed"))
        table = Table(box=None, padding=(0, 1), expand=True)
        for name in ("Request", "Question", "Activity", "Elapsed", "Last event"):
            table.add_column(name, overflow="ellipsis", no_wrap=True,
                             ratio=2 if name == "Question" else 3 if name == "Activity" else None)
        visible = sorted(self.rows.items(), key=lambda pair: pair[1].outcome is not None)
        limit = max(1, min(10, self.console.height - 6))
        for request_id, row in visible[:limit]:
            elapsed = row.elapsed if row.outcome is not None else now - row.started
            if row.outcome is not None:
                activity = Text(row.status, style="green" if row.outcome == "success" else "red")
            else:
                row.spinner.update(text=Text(row.status))
                activity = row.spinner
            table.add_row(Text(f"#{request_id}"), Text(row.question), activity,
                          Text(f"{elapsed:.1f}s"), Text("—" if row.outcome else f"{now - row.last_update:.1f}s ago"))
        extras = [] if len(visible) <= limit else [Text(f"{len(visible) - limit} more requests; all results appear below.", style="dim")]
        return Group(heading, table, *extras)

    def tick(self) -> None:
        if not self.output_available:
            self.pending.clear()
            return
        try:
            if self.live:
                self.live.update(self.render(), refresh=True)
                self.pending.clear()
            else:
                had_messages = bool(self.pending)
                while self.pending:
                    self.console.print(Text(self.pending.popleft()), highlight=False)
                now = perf_counter()
                active = [(key, row) for key, row in self.rows.items() if row.outcome is None]
                if active and not had_messages and now - self.last_heartbeat >= self.heartbeat_seconds:
                    details = "; ".join(f"#{key} {row.status} ({now - row.started:.1f}s)" for key, row in active[:5])
                    if len(active) > 5:
                        details += f"; +{len(active) - 5} more active"
                    self.console.print(Text("Still waiting — " + details), highlight=False)
                    self.last_heartbeat = now
        except (OSError, ValueError):
            # A closed output pipe should not discard the benchmark's JSON.
            self.output_available = False

    async def refresh_loop(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(0.25)

    def __enter__(self):
        if self.console.is_terminal and not self.console.is_dumb_terminal:
            self.live = Live(self.render(), console=self.console, auto_refresh=False, transient=True,
                             redirect_stdout=False, redirect_stderr=False)
            try:
                self.live.start(refresh=True)
            except (OSError, ValueError):
                self.output_available = False
                self.live = None
        return self

    def __exit__(self, *args):
        self.tick()
        if self.live:
            try:
                self.live.stop()
            except (OSError, ValueError):
                pass
