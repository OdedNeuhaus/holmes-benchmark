import asyncio
from io import StringIO

from rich.console import Console

from holmes_benchmark.config import Config
from conftest import event


def test_live_state_tracks_overlapping_tools_and_sanitizes_text():
    from holmes_benchmark.progress import ProgressDisplay
    from holmes_benchmark.runner import ProgressUpdate

    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=140, color_system=None)
    display = ProgressDisplay(Config("http://host/api/chat", ("Check [bold]secret\x1b[2J",), token="secret"), console=console)
    display.update(ProgressUpdate(1, "started", 0))
    display.update(ProgressUpdate(1, "start_tool_calling", 0.1, "a", "kubectl_secret"))
    display.update(ProgressUpdate(1, "start_tool_calling", 0.2, "b", "prometheus"))
    console.print(display.render())
    text = stream.getvalue()
    assert "kubectl_[REDACTED]" in text and "prometheus" in text
    assert "[bold]" in text  # Question is literal text, not Rich markup.
    assert "secret" not in text and "\x1b[2J" not in text
    stream.seek(0)
    stream.truncate()
    display.update(ProgressUpdate(1, "tool_calling_result", 0.3, "a", "kubectl_secret", "success"))
    console.print(display.render())
    assert "Running: prometheus" in stream.getvalue()
    assert "kubectl_" not in stream.getvalue()
    display.update(ProgressUpdate(1, "finished", 1.2, outcome="success"))
    stream.seek(0)
    stream.truncate()
    console.print(display.render())
    assert "1/1 finished" in stream.getvalue()
    assert "success" in stream.getvalue()


def test_plain_progress_logs_events_and_quiet_period_heartbeat():
    from holmes_benchmark.progress import ProgressDisplay
    from holmes_benchmark.runner import ProgressUpdate

    stream = StringIO()
    display = ProgressDisplay(Config("http://host/api/chat", ("Question",)),
                              console=Console(file=stream, force_terminal=False), heartbeat_seconds=0)
    with display:
        display.update(ProgressUpdate(1, "started", 0))
        display.tick()
        assert "#1" in stream.getvalue() and "Waiting" in stream.getvalue()
        display.tick()
        assert "Still waiting" in stream.getvalue()
        display.update(ProgressUpdate(1, "start_tool_calling", 0.1, "a", "kubectl"))
        display.tick()
        assert "kubectl" in stream.getvalue()
        display.update(ProgressUpdate(1, "finished", 0.5, outcome="timeout"))
    assert "timeout" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()


def test_runner_emits_tool_updates_before_completion(sse_server):
    from holmes_benchmark.runner import run_benchmark

    updates = []
    with sse_server({"A": (200, "text/event-stream", [
        (0, event("start_tool_calling", {"id": "a", "tool_name": "kubectl"})),
        (0.1, event("tool_calling_result", {"tool_call_id": "a", "name": "kubectl",
                                            "result": {"status": "success"}})),
        (0.1, event("ai_answer_end", {"analysis": "done"})),
    ])}) as (url, _):
        async def scenario():
            task = asyncio.create_task(run_benchmark(Config(url, ("A",)), on_progress=updates.append))
            while not any(u.kind == "start_tool_calling" for u in updates) and not task.done():
                await asyncio.sleep(0.005)
            assert not task.done()
            return await task
        batch = asyncio.run(scenario())
    assert batch.requests[0].outcome == "success"
    assert [u.kind for u in updates] == ["started", "connected", "start_tool_calling", "tool_calling_result", "finished"]
    assert updates[2].tool_name == "kubectl" and updates[3].tool_status == "success"
    assert updates[-1].outcome == "success"


def test_failing_progress_observer_does_not_fail_benchmark(sse_server):
    from holmes_benchmark.runner import run_benchmark

    def failed_observer(update):
        raise OSError("output closed")

    with sse_server() as (url, _):
        batch = asyncio.run(run_benchmark(Config(url, ("A",)), on_progress=failed_observer))
    assert batch.requests[0].outcome == "success"


def test_partial_event_keeps_incomplete_outcome_with_progress(sse_server):
    from holmes_benchmark.runner import run_benchmark

    updates = []
    partial = event("ai_message", {"content": ["partial"]}).rstrip(b"\n")
    with sse_server({"A": (200, "text/event-stream", [(0, partial)])}) as (url, _):
        batch = asyncio.run(run_benchmark(Config(url, ("A",)), on_progress=updates.append))
    assert batch.requests[0].outcome == "incomplete"
    assert [u.kind for u in updates] == ["started", "connected", "finished"]
    assert updates[-1].outcome == "incomplete"
