"""Console entry point and output lifecycle."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryFile
from uuid import uuid4

from .config import parse_config
from .report import build_report, print_summary, redact, terminal_text, write_excel, write_json
from .runner import run_benchmark


def _output_directory() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = Path("results") / f"{stamp}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, mode=0o700)
    # Actually write a byte: permissions checks alone can be misleading on mounts.
    with TemporaryFile(dir=directory) as probe:
        probe.write(b"\0")
        probe.flush()
    return directory


def main(argv: list[str] | None = None) -> int:
    config = parse_config(argv)

    def error(message):
        print(terminal_text(redact(message, config.token), 500), file=sys.stderr)

    try:
        directory = _output_directory()
    except OSError as exc:
        error(f"Cannot prepare output directory: {exc}")
        return 1
    print(f"Running {config.parallel} concurrent request(s), "
          f"{len(config.questions)} question(s), {config.timeout:g}s deadline each…", flush=True)
    try:
        batch = asyncio.run(run_benchmark(config))
        report = build_report(config, batch)
        json_path = write_json(report, directory)
        print_summary(report)
        print(f"\nJSON: {json_path.resolve()}", flush=True)
        if config.export_to_excel:
            try:
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
