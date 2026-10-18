"""Command-line configuration; validation happens before any network traffic."""

import argparse
from dataclasses import dataclass, field
import math
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    api_url: str
    questions: tuple[str, ...]
    parallel: int = 1
    timeout: float = 300.0
    model: str | None = None
    token: str | None = field(default=None, repr=False)
    export_to_excel: bool = False
    no_progress: bool = False


def parse_config(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="holmes-benchmark",
        description="Benchmark one concurrent batch of fresh HolmesGPT conversations.",
        epilog="Questions cycle in order. JSON is always saved under ./results/. No retries.",
    )
    parser.add_argument("-q", "--question", action="append", required=True,
                        help="question to ask; repeat to cycle different questions")
    parser.add_argument("-p", "--parallel", type=int, default=1, metavar="N",
                        help="number of concurrent requests (default: 1)")
    parser.add_argument("--api-url", default=os.environ.get("HOLMES_API_URL"),
                        help="full chat endpoint (env: HOLMES_API_URL)")
    parser.add_argument("--model", default=os.environ.get("HOLMES_MODEL") or None,
                        help="server model name (env: HOLMES_MODEL; default: server choice)")
    parser.add_argument("--timeout", default=os.environ.get("HOLMES_TIMEOUT", "300"),
                        metavar="SECONDS", help="total per-request deadline (env: HOLMES_TIMEOUT; default: 300)")
    parser.add_argument("--export-to-excel", action="store_true",
                        help="also save a formatted report.xlsx")
    parser.add_argument("--no-progress", action="store_true",
                        help="disable live request activity and progress logs")
    args = parser.parse_args(argv)
    if args.parallel < 1:
        parser.error("--parallel must be a positive integer")
    if any(not q.strip() for q in args.question):
        parser.error("questions must not be empty or whitespace only")
    if len(args.question) > args.parallel:
        parser.error("provide no more questions than --parallel requests")
    try:
        timeout = float(args.timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        parser.error("--timeout / HOLMES_TIMEOUT must be a finite positive number")
    try:
        url = args.api_url or ""
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError
        _ = parts.port  # Validate malformed or out-of-range ports.
    except ValueError:
        parser.error("set --api-url or HOLMES_API_URL to a full HTTP(S) endpoint without userinfo or fragment")
    if args.model is not None and not args.model.strip():
        parser.error("--model / HOLMES_MODEL must not be blank")
    token = os.environ.get("HOLMES_API_TOKEN") or None
    if token and any(not 33 <= ord(c) <= 126 for c in token):
        parser.error("HOLMES_API_TOKEN must contain only visible ASCII characters, without spaces")
    return Config(api_url=url, questions=tuple(args.question), parallel=args.parallel,
                  timeout=timeout, model=args.model, token=token,
                  export_to_excel=args.export_to_excel, no_progress=args.no_progress)
