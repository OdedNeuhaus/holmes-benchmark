"""One fixed UTC+3 clock for benchmark timestamps, independent of host/DST."""

from datetime import datetime, timedelta, timezone


BENCHMARK_TIMEZONE = timezone(timedelta(hours=3), name="UTC+3")
TIMEZONE_LABEL = "UTC+3"


def now() -> datetime:
    return datetime.now(BENCHMARK_TIMEZONE)


def timestamp_now() -> str:
    return now().isoformat(timespec="milliseconds")


def format_timestamp(value: str | None) -> str | None:
    """Convert an aware benchmark timestamp, including older UTC results."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Benchmark timestamps must include a timezone offset")
    return parsed.astimezone(BENCHMARK_TIMEZONE).isoformat(timespec="milliseconds")
