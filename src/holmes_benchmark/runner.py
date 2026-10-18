"""Concurrent streaming requests and client-observed measurements."""

import asyncio
from dataclasses import dataclass, field
import json
import math
from time import perf_counter
from typing import Any, Callable

import httpx

from .config import Config
from .sse import Event, iter_events, iter_lines
from .timestamps import timestamp_now


@dataclass
class RequestResult:
    request_id: int
    question: str
    requested_model: str | None = None
    outcome: str = "cancelled"
    started_at: str | None = None
    finished_at: str | None = None
    http_status: int | None = None
    first_text_seconds: float | None = None
    completion_seconds: float | None = None
    elapsed_seconds: float | None = None
    answer: str | None = None
    error: str | None = "Cancelled before request started"
    tool_call_count: int = 0
    tool_error_count: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    last_reported_usage: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    response_body: str | None = None


@dataclass
class Batch:
    requests: list[RequestResult]
    started_at: str
    finished_at: str
    duration_seconds: float
    interrupted: bool = False


@dataclass(frozen=True)
class ProgressUpdate:
    request_id: int
    kind: str
    elapsed_seconds: float
    tool_id: str | None = None
    tool_name: str | None = None
    tool_status: str | None = None
    outcome: str | None = None


ProgressObserver = Callable[[ProgressUpdate], None]


def _notify(observer: ProgressObserver | None, update: ProgressUpdate) -> None:
    if observer is not None:
        try:
            observer(update)
        except Exception:
            # Optional display failures must not invalidate measurements.
            pass


def _notify_event(observer: ProgressObserver | None, result: RequestResult, event: Event, elapsed: float) -> None:
    if observer is None or event.event not in {
        "ai_message", "token_count", "start_tool_calling", "tool_calling_result",
        "conversation_history_compaction_start", "conversation_history_compacted",
    }:
        return
    data = result.raw_events[-1]["data"]
    if not isinstance(data, dict):
        return
    if event.event == "ai_message" and not (data.get("content") or "").strip():
        return
    call_id = data.get("id") if event.event == "start_tool_calling" else data.get("tool_call_id")
    tool_result = data.get("result")
    _notify(observer, ProgressUpdate(
        result.request_id, event.event, elapsed,
        tool_id=str(call_id) if call_id is not None else None,
        tool_name=data.get("tool_name") or data.get("name"),
        tool_status=tool_result.get("status") if isinstance(tool_result, dict) else None,
    ))


class MalformedResponse(ValueError):
    pass


KNOWN_EVENTS = {
    "ai_message", "ai_answer_end", "token_count", "start_tool_calling",
    "tool_calling_result", "error", "approval_required",
    "conversation_history_compaction_start", "conversation_history_compacted",
}


def _tool_event(result: RequestResult, name: str, payload: dict, elapsed: float) -> None:
    call_id = payload.get("id") if name == "start_tool_calling" else payload.get("tool_call_id")
    tool = next((t for t in result.tools if call_id is not None and t["id"] == call_id), None)
    if tool is None:
        tool = {"id": call_id, "name": payload.get("tool_name") or payload.get("name"),
                "started_seconds": None, "finished_seconds": None, "result": None}
        result.tools.append(tool)
    if name == "start_tool_calling":
        if tool["started_seconds"] is None:
            tool["started_seconds"] = elapsed
    else:
        tool["finished_seconds"] = elapsed
        tool["result"] = payload.get("result")
    result.tool_call_count = len(result.tools)
    result.tool_error_count = sum(
        isinstance(t["result"], dict) and (
            t["result"].get("status") in ("error", "approval_required")
            or bool(t["result"].get("error"))) for t in result.tools
    )


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _consume_event(result: RequestResult, event: Event, elapsed: float) -> bool:
    """Retain every data event; return True when this request is terminal."""
    try:
        payload = json.loads(event.raw_data, parse_float=_finite_float, parse_constant=_finite_float)
    except ValueError:
        payload = None
    result.raw_events.append({
        "event": event.event, "id": event.id, "data": payload,
        "raw_data": event.raw_data, "raw_lines": event.raw_lines,
        "complete": event.complete, "received_at": timestamp_now(), "elapsed_seconds": elapsed,
    })
    if not event.complete:
        result.outcome, result.error = "incomplete", "Stream ended inside an SSE event"
        return True
    if event.event not in KNOWN_EVENTS:
        return False
    if not isinstance(payload, dict):
        raise MalformedResponse(f"{event.event} must contain a JSON object")
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        result.metadata.update(metadata)
        if isinstance(metadata.get("usage"), dict):
            result.last_reported_usage = metadata["usage"]
    if event.event == "ai_message":
        content = payload.get("content")
        if content is not None and not isinstance(content, str):
            raise MalformedResponse("ai_message content must be text or null")
        if content and content.strip() and result.first_text_seconds is None:
            result.first_text_seconds = elapsed
    elif event.event == "ai_answer_end":
        answer = payload.get("analysis")
        if not isinstance(answer, str):
            raise MalformedResponse("ai_answer_end must include a text analysis")
        result.answer = answer
        if answer.strip() and result.first_text_seconds is None:
            result.first_text_seconds = elapsed
        result.completion_seconds = elapsed
        result.outcome, result.error = "success", None
        return True
    elif event.event in ("start_tool_calling", "tool_calling_result"):
        _tool_event(result, event.event, payload, elapsed)
    elif event.event == "error":
        result.outcome = "stream_error"
        result.error = str(payload.get("description") or payload.get("msg") or payload)
        return True
    elif event.event == "approval_required":
        result.outcome = "approval_required"
        result.error = "Incomplete: server requested external approval or a frontend tool"
        return True
    return False


async def _request(client: httpx.AsyncClient, config: Config,
                   result: RequestResult, barrier: asyncio.Barrier,
                   on_progress: ProgressObserver | None = None) -> None:
    started = None
    try:
        await barrier.wait()
        started = perf_counter()
        result.started_at = timestamp_now()
        result.outcome, result.error = "incomplete", "Stream ended without ai_answer_end"
        _notify(on_progress, ProgressUpdate(result.request_id, "started", 0))
        body: dict[str, Any] = {"ask": result.question, "stream": True}
        if config.model is not None:
            body["model"] = config.model
        async with asyncio.timeout(config.timeout):
            async with client.stream("POST", config.api_url, json=body) as response:
                result.http_status = response.status_code
                _notify(on_progress, ProgressUpdate(result.request_id, "connected", perf_counter() - started))
                if not response.is_success:
                    result.outcome = "http_error"
                    result.response_body = (await response.aread()).decode("utf-8", errors="replace")
                    result.error = f"HTTP {response.status_code}: {result.response_body}"
                    return
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                    result.response_body = (await response.aread()).decode("utf-8", errors="replace")
                    raise MalformedResponse("Expected Content-Type text/event-stream")
                async for event in iter_events(iter_lines(response.aiter_bytes())):
                    elapsed = perf_counter() - started
                    terminal = _consume_event(result, event, elapsed)
                    if terminal:
                        return
                    _notify_event(on_progress, result, event, elapsed)
    except asyncio.CancelledError:
        result.outcome, result.error = "cancelled", "Interrupted before completion"
    except (TimeoutError, httpx.TimeoutException):
        result.outcome, result.error = "timeout", f"Exceeded {config.timeout:g}s total request deadline"
    except (MalformedResponse, UnicodeError) as exc:
        result.outcome, result.error = "malformed_response", str(exc)
    except httpx.HTTPError as exc:
        result.outcome, result.error = "network_error", f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        # An unexpected problem in one response must not discard its siblings.
        result.outcome, result.error = "client_error", f"{type(exc).__name__}: {exc}"
    finally:
        result.finished_at = timestamp_now()
        if started is not None:
            result.elapsed_seconds = perf_counter() - started
        _notify(on_progress, ProgressUpdate(result.request_id, "finished", result.elapsed_seconds or 0,
                                            outcome=result.outcome))


async def run_benchmark(config: Config, on_progress: ProgressObserver | None = None) -> Batch:
    results = [RequestResult(i + 1, config.questions[i % len(config.questions)], config.model)
               for i in range(config.parallel)]
    headers = {"Accept": "text/event-stream"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    limits = httpx.Limits(max_connections=config.parallel, max_keepalive_connections=config.parallel)
    async with httpx.AsyncClient(headers=headers, limits=limits, timeout=None,
                                follow_redirects=False) as client:
        started_at, started = timestamp_now(), perf_counter()
        barrier = asyncio.Barrier(config.parallel)
        tasks = [asyncio.create_task(_request(client, config, r, barrier, on_progress)) for r in results]
        group = asyncio.gather(*tasks)
        interrupted = False
        try:
            await asyncio.shield(group)
        except asyncio.CancelledError:
            interrupted = True
            for task in tasks:
                task.cancel()
            await group
        duration = perf_counter() - started
        finished_at = timestamp_now()
    return Batch(results, started_at, finished_at, duration, interrupted)
