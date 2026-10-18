"""Console entry point and output lifecycle."""

import asyncio
from contextlib import suppress
from pathlib import Path
import sys
from tempfile import TemporaryFile
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import parse_config
from .report import build_report, print_summary, redact, terminal_text, write_excel, write_json
from .runner import run_benchmark
from .progress import ProgressDisplay
from .timestamps import now, TIMEZONE_LABEL


def _output_directory() -> Path:
    stamp = now().strftime("%Y%m%dT%H%M%S%f%z")
    directory = Path("results") / f"{stamp}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, mode=0o700)
    # Actually write a byte: permissions checks alone can be misleading on mounts.
    with TemporaryFile(dir=directory) as probe:
        probe.write(b"\0")
        probe.flush()
    return directory


async def _run(config):
    if config.no_progress:
        return await run_benchmark(config)
    with ProgressDisplay(config) as display:
        refresh = asyncio.create_task(display.refresh_loop())
        try:
            return await run_benchmark(config, on_progress=display.update)
        finally:
            refresh.cancel()
            with suppress(asyncio.CancelledError):
                await refresh


def main(argv: list[str] | None = None) -> int:
    config = parse_config(argv)

    def error(message):
        print(terminal_text(redact(message, config.token), 500), file=sys.stderr)

    try:
        directory = _output_directory()
    except OSError as exc:
        error(f"Cannot prepare output directory: {exc}")
        return 1
    console = Console()
    console.print()
    console.print(Panel(
        Text.assemble(("Holmes", "bold bright_white"), ("GPT", "bold cyan"),
                      (" Benchmarking", "bold bright_white"),
                      ("\nASK  /  OBSERVE  /  MEASURE", "dim")),
        border_style="cyan", padding=(1, 3), expand=False,
    ))
    console.print()
    print(f"Running {config.parallel} concurrent request(s), "
          f"{len(config.questions)} question(s), {config.timeout:g}s deadline each… "
          f"Times: {TIMEZONE_LABEL}", flush=True)
    try:
        batch = asyncio.run(_run(config))
        report = build_report(config, batch)
        json_path = write_json(report, directory)
        print_summary(report, show_details=config.show_summary, console=console)
        print(f"\nJSON: {json_path.resolve()}", flush=True)
        if config.export_to_excel:
            try:
                if not config.no_progress:
                    print("Saving Excel report…", file=sys.stderr, flush=True)
                excel_path = write_excel(report, directory)
                print(f"Excel: {excel_path.resolve()}")
            except Exception as exc:
                error(f"Excel export failed; JSON is saved at {json_path.resolve()}: {exc}")
                return 130 if batch.interrupted else 1
        if batch.interrupted:
            return 130
        return 0 if report["summary"]["unsuccessful_requests"] == 0 else 1
    except KeyboardInterrupt:
        # The first SIGINT during the async run is handled by the runner; a
        # subsequent interrupt (or one during export) can force immediate exit.
        error(f"Interrupted. Any completed output is in {directory.resolve()}")
        return 130
    except Exception as exc:
        error(f"Benchmark or output failed: {exc}")
        return 1
