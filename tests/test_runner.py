import asyncio
from collections import Counter

import pytest

from conftest import event
from holmes_benchmark.config import Config


def run(config):
    from holmes_benchmark.runner import run_benchmark
    return asyncio.run(run_benchmark(config))


def test_concurrent_batch_cycles_questions_and_sends_contract(sse_server):
    with sse_server(barrier=5) as (url, received):
        batch = run(Config(url, ("A", "B"), parallel=5, token="test-token", model="fast"))
    assert [r.question for r in batch.requests] == ["A", "B", "A", "B", "A"]
    assert all(r.outcome == "success" for r in batch.requests)
    assert len(received) == 5
    assert Counter(r["body"]["ask"] for r in received) == {"A": 3, "B": 2}
    assert max(r["at"] for r in received) - min(r["at"] for r in received) < 1
    assert all(r["body"] == {"ask": r["body"]["ask"], "stream": True, "model": "fast"}
               for r in received)
    assert all(r["headers"]["Authorization"] == "Bearer test-token" for r in received)
    assert all(r["path"] == "/api/chat" for r in received)
    assert batch.duration_seconds >= max(r.completion_seconds for r in batch.requests)


def test_first_text_tools_and_last_reported_usage(sse_server):
    chunks = [
        (0, b": keepalive\n\n"),
        (0, event("ai_message", {"content": "", "reasoning": "not visible text"})),
        (0, event("token_count", {"metadata": {"usage": {"total_tokens": 10}}})),
        (0.03, event("ai_message", {"content": "Investigating"})),
        (0, event("start_tool_calling", {"id": "call_1", "tool_name": "kubectl"})),
        (0, event("tool_calling_result", {"tool_call_id": "call_1", "name": "kubectl",
                   "result": {"status": "error", "error": "access denied", "data": "", "params": {}}})),
        (0, event("token_count", {"metadata": {"usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}})),
        (0.12, event("ai_answer_end", {"analysis": "Final answer", "metadata": {"other": "kept"}})),
    ]
    with sse_server({"A": (200, "text/event-stream", chunks)}) as (url, received):
        result = run(Config(url, ("A",))).requests[0]
    assert result.outcome == "success"
    assert result.answer == "Final answer"
    assert result.completion_seconds - result.first_text_seconds >= 0.1
    assert result.tool_call_count == 1
    assert result.tool_error_count == 1
    assert result.last_reported_usage == {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    assert result.metadata["other"] == "kept"
    assert [e["event"] for e in result.raw_events] == [
        "ai_message", "token_count", "ai_message", "start_tool_calling",
        "tool_calling_result", "token_count", "ai_answer_end",
    ]
    assert all(e["received_at"] and e["elapsed_seconds"] >= 0 for e in result.raw_events)
    assert "Authorization" not in received[0]["headers"]
    assert "model" not in received[0]["body"]


def test_fragmented_utf8_multiline_sse_unknown_events_and_final_fallback(sse_server):
    raw = ('\ufeff: heartbeat\r\n\r\nevent: future_event\r\ndata: opaque payload\r\n\r\n'
           'event: ai_answer_end\r\nid: 7\r\ndata: {"analysis":\r\ndata: "שלום 😀"}\r\n\r\n').encode()
    chunks = [(0, raw[i:i + 1]) for i in range(len(raw))]
    with sse_server({"A": (200, "text/event-stream; charset=utf-8", chunks)}) as (url, _):
        result = run(Config(url, ("A",))).requests[0]
    assert result.outcome == "success"
    assert result.answer == "שלום 😀"
    assert result.first_text_seconds == result.completion_seconds
    assert result.last_reported_usage is None
    assert result.raw_events[0]["raw_data"] == "opaque payload"
    assert result.raw_events[-1]["id"] == "7"


@pytest.mark.parametrize("status,ctype,chunks,outcome", [
    (401, "application/json", [(0, b'{"detail":"unauthorized"}')], "http_error"),
    (429, "application/json", [(0, b'{"detail":"rate limited"}')], "http_error"),
    (503, "text/plain", [(0, b"unavailable")], "http_error"),
    (200, "text/event-stream", [(0, event("error", {"msg": "model failed", "error_code": 1}))], "stream_error"),
    (200, "text/event-stream", [(0, event("approval_required", {"pending_approvals": [{"id": "1"}]}))], "approval_required"),
    (200, "text/event-stream", [(0, event("ai_message", {"content": "partial"}))], "incomplete"),
    (200, "text/event-stream", [(0, b'event: ai_answer_end\ndata: {"analysis":"truncated"}')], "incomplete"),
    (200, "text/event-stream", [(0, b'event: ai_answer_end\ndata: {broken}\n\n')], "malformed_response"),
    (200, "text/event-stream", [(0, event("ai_answer_end", []))], "malformed_response"),
    (200, "text/event-stream", [(0, event("ai_answer_end", {"analysis": None}))], "malformed_response"),
    (200, "application/json", [(0, b'{"analysis":"not a stream"}')], "malformed_response"),
])
def test_failures_are_isolated_and_never_retried(sse_server, status, ctype, chunks, outcome):
    with sse_server({"bad": (status, ctype, chunks)}) as (url, received):
        batch = run(Config(url, ("bad", "good"), parallel=2))
    bad, good = batch.requests
    assert bad.outcome == outcome
    assert bad.http_status == status
    assert bad.error
    assert bad.completion_seconds is None
    assert bad.elapsed_seconds >= 0
    assert good.outcome == "success"
    assert len(received) == 2


@pytest.mark.parametrize("chunks", [
    [(2, event("ai_answer_end", {"analysis": "late"}))],
    [(0.03, b": heartbeat\n\n")] * 30,
])
def test_timeout_is_total_deadline_even_with_heartbeats(sse_server, chunks):
    with sse_server({"slow": (200, "text/event-stream", chunks)}) as (url, _):
        result = run(Config(url, ("slow",), timeout=0.12)).requests[0]
    assert result.outcome == "timeout"
    assert 0.10 <= result.elapsed_seconds < 0.7
    assert result.completion_seconds is None


def test_cancellation_preserves_successes_and_partial_events(sse_server):
    from holmes_benchmark.runner import run_benchmark

    async def scenario(url):
        task = asyncio.create_task(run_benchmark(Config(url, ("fast", "slow"), parallel=2)))
        await asyncio.sleep(0.15)
        task.cancel()
        return await task

    with sse_server({"slow": (200, "text/event-stream", [
        (0, event("ai_message", {"content": "started"})),
        (2, event("ai_answer_end", {"analysis": "late"})),
    ])}) as (url, _):
        batch = asyncio.run(scenario(url))
    assert batch.interrupted
    assert [r.outcome for r in batch.requests] == ["success", "cancelled"]
    assert batch.requests[1].raw_events[0]["data"]["content"] == "started"
    assert batch.requests[1].started_at and batch.requests[1].finished_at


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "1e10000"])
def test_nonfinite_json_isolated_without_losing_other_results(sse_server, tmp_path, literal):
    from holmes_benchmark.report import build_report, write_json

    data = ('event: token_count\ndata: {"metadata":{"usage":{"total_tokens":'
            + literal + '}}}\n\n').encode()
    with sse_server({"bad": (200, "text/event-stream", [(0, data)])}) as (url, _):
        config = Config(url, ("bad", "good"), parallel=2)
        batch = run(config)
    assert [r.outcome for r in batch.requests] == ["malformed_response", "success"]
    write_json(build_report(config, batch), tmp_path)
    assert literal in (tmp_path / "results.json").read_text()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_unicode_line_separators_inside_json_are_not_sse_boundaries(sse_server, separator, newline):
    text = f"First{separator}second שלום"
    raw = event("ai_message", {"content": text}) + event("ai_answer_end", {"analysis": text})
    raw = raw.replace(b"\n", newline)
    chunks = [(0, raw[i:i + 1]) for i in range(len(raw))]
    with sse_server({"A": (200, "text/event-stream", chunks)}) as (url, _):
        result = run(Config(url, ("A",))).requests[0]
    assert result.outcome == "success", result.error
    assert result.answer == text
    assert len(result.raw_events) == 2
    assert result.raw_events[0]["data"]["content"] == text
    assert result.first_text_seconds <= result.completion_seconds


def test_invalid_utf8_is_a_malformed_response(sse_server):
    with sse_server({"A": (200, "text/event-stream", [(0, b'event: ai_answer_end\ndata: {"analysis":"\xff"}\n\n')])}) as (url, _):
        result = run(Config(url, ("A",))).requests[0]
    assert result.outcome == "malformed_response"
