"""Build one portable report shared by all output formats."""

from dataclasses import asdict
import json
import math
from pathlib import Path
import statistics
from typing import Any
import unicodedata
from urllib.parse import quote

from .config import Config
from .excel import write_excel  # Public export alongside write_json.
from .runner import Batch, KNOWN_EVENTS, RequestResult
from .timestamps import format_timestamp, TIMEZONE_LABEL


def redact(value: Any, token: str | None) -> Any:
    """Remove the configured credential, including echoes inside encoded events."""
    if not token:
        return value
    variants = sorted({token, json.dumps(token)[1:-1], quote(token, safe="")}, key=len, reverse=True)

    def clean(item):
        if isinstance(item, str):
            for secret in variants:
                item = item.replace(secret, "[REDACTED]")
            return item
        if isinstance(item, dict):
            return {clean(k): clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        return item

    return clean(value)


def _safe_request(result: RequestResult, token: str | None) -> dict:
    record = asdict(result)
    for key in ("started_at", "finished_at"):
        record[key] = format_timestamp(record[key])
    # Only externally sourced content is secret-bearing. Application keys,
    # outcomes, IDs, timings, and counters must retain their schema semantics.
    for key in ("question", "requested_model", "answer", "error", "response_body",
                "last_reported_usage", "metadata"):
        record[key] = redact(record[key], token)
    record["tools"] = [{key: redact(value, token) for key, value in tool.items()}
                       for tool in record["tools"]]
    for event in record["raw_events"]:
        if "received_at" in event:
            event["received_at"] = format_timestamp(event["received_at"])
        for key in ("id", "data", "raw_data", "raw_lines"):
            if key in event:
                event[key] = redact(event[key], token)
        if event.get("event") not in KNOWN_EVENTS:
            event["event"] = redact(event.get("event"), token)
    return record


def build_report(config: Config, batch: Batch) -> dict:
    timings = sorted(r.completion_seconds for r in batch.requests
                     if r.outcome == "success" and r.completion_seconds is not None)
    total, count = len(batch.requests), len(timings)
    stats = {"count": count, "min": None, "mean": None, "median": None, "p95": None, "max": None}
    if timings:
        stats.update(min=timings[0], mean=statistics.mean(timings), median=statistics.median(timings),
                     p95=timings[math.ceil(0.95 * count) - 1], max=timings[-1])
    report = {
        "schema_version": 1,
        "started_at": format_timestamp(batch.started_at),
        "finished_at": format_timestamp(batch.finished_at),
        "interrupted": batch.interrupted,
        "settings": {"timezone": TIMEZONE_LABEL, "api_url": redact(config.api_url, config.token),
                     "questions": redact(list(config.questions), config.token),
                     "parallel": config.parallel, "timeout_seconds": config.timeout,
                     "requested_model": redact(config.model, config.token), "stream": True,
                     "export_to_excel": config.export_to_excel, "progress_enabled": not config.no_progress,
                     "show_summary": config.show_summary},
        "measurement_notes": {
            "first_text": "First nonempty ai_message.content, or final analysis; not first-token latency.",
            "completion": "Client-observed time through ai_answer_end; successful requests only.",
            "usage": "Last reported usage snapshot, not total billed usage across the investigation.",
            "missing": "Null means unavailable; timings are seconds measured with a monotonic clock.",
            "p95": "Nearest rank across successful requests; small batches have limited statistical value.",
        },
        "summary": {"total_requests": total, "successful_requests": count,
                    "unsuccessful_requests": total - count, "success_rate": count / total if total else 0,
                    "batch_duration_seconds": batch.duration_seconds,
                    "successful_requests_per_second": count / batch.duration_seconds if batch.duration_seconds > 0 else None,
                    "completion_seconds": stats},
        "requests": [_safe_request(r, config.token) for r in batch.requests],
    }
    return report


def write_json(report: dict, directory: Path) -> Path:
    destination = directory / "results.json"
    temporary = directory / "results.json.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        # ASCII escaping also safely preserves unusual Unicode from server responses.
        json.dump(report, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(destination)
    return destination


def terminal_text(value: Any, limit: int = 60) -> str:
    text = " ".join(str(value).split())
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def print_summary(report: dict, *, show_details: bool = True, console=None) -> None:
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = console or Console()

    def number(value):
        return "N/A" if value is None else f"{value:.3f}"

    def tokens(usage, key):
        value = usage.get(key)
        try:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return "N/A"
        except OverflowError:
            return "N/A"
        return f"{value:,}" if value < 1_000_000_000 else f"{value:.3g}"

    console.print()
    if show_details:
        table = Table(title="Benchmark results", title_style="bold cyan", box=box.ROUNDED,
                      border_style="dim cyan", header_style="bold cyan", padding=(0, 1))
        for label in ("#", "Outcome", "First\n(s)", "Answer\n(s)", "Elapsed\n(s)",
                      "Tools /\nerrors", "Tokens\nin / out / total"):
            table.add_column(label, justify="left" if label == "Outcome" else "right")
        for result in report["requests"]:
            usage = result["last_reported_usage"] or {}
            table.add_row(
                str(result["request_id"]),
                Text(result["outcome"], style="green" if result["outcome"] == "success" else "red"),
                number(result["first_text_seconds"]), number(result["completion_seconds"]),
                number(result["elapsed_seconds"]),
                f"{result['tool_call_count']} / {result['tool_error_count']}",
                " / ".join(tokens(usage, key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")),
            )
        console.print(table)
        console.print("Last reported tokens (input / output / total), not investigation totals.", style="dim")
        console.print("N/A = unavailable. First = first assistant text; Answer = completed answer.", style="dim")
    for result in report["requests"]:
        if show_details or result["outcome"] != "success":
            console.print(Text(f"#{result['request_id']}  {result['outcome']}  ·  {terminal_text(result['question'], 100)}"))
        if result["error"]:
            console.print(Text(f"    {terminal_text(result['error'], 180)}", style="red"))
    summary = report["summary"]
    times = summary["completion_seconds"]
    console.print(Text(f"\nSuccess: {summary['successful_requests']}/{summary['total_requests']} "
          f"({summary['success_rate']:.1%}) | Batch: {number(summary['batch_duration_seconds'])}s | "
          f"Successful requests/s: {number(summary['successful_requests_per_second'])}"))
    console.print(Text(f"Completed answers (n={times['count']}): " + " | ".join(
        f"{name}: {number(times[name])}s" for name in ("min", "mean", "median", "p95", "max"))))
    if report["interrupted"]:
        console.print("Interrupted; partial results saved.")
