import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import queue
import threading

import pytest

from conftest import event


def cli_env():
    env = dict(os.environ)
    for name in ("HOLMES_API_URL", "HOLMES_API_TOKEN", "HOLMES_MODEL", "HOLMES_TIMEOUT"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


def invoke(args, directory, env=None):
    return subprocess.run([sys.executable, "-m", "holmes_benchmark", *args],
                          cwd=directory, env=env or cli_env(), capture_output=True,
                          text=True, timeout=10)


def test_cli_exports_json_and_excel_and_keeps_credentials_out(sse_server, tmp_path):
    secret = "example-secret-credential"
    with sse_server({"A": (200, "text/event-stream", [
        (0, event("ai_answer_end", {"analysis": f"Echo {secret}", "metadata": {"usage": {"total_tokens": 5}}})),
    ])}, barrier=2) as (url, requests):
        env = cli_env()
        env.update(HOLMES_API_URL=url, HOLMES_API_TOKEN=secret)
        process = invoke(["-q", "A", "-p", "2", "--export-to-excel"], tmp_path, env)
    assert process.returncode == 0, process.stderr
    assert "Success: 2/2" in process.stdout
    assert "results.json" in process.stdout and "report.xlsx" in process.stdout
    saved = list(tmp_path.glob("results/*/results.json"))
    assert len(saved) == 1
    report = json.loads(saved[0].read_text())
    assert report["requests"][0]["answer"] == "Echo [REDACTED]"
    assert saved[0].with_name("report.xlsx").exists()
    assert secret not in process.stdout + process.stderr + saved[0].read_text()
    assert len(requests) == 2


def test_cli_failure_exit_and_unique_runs(sse_server, tmp_path):
    with sse_server({"bad": (503, "text/plain", [(0, b"unavailable")])}) as (url, requests):
        for _ in range(2):
            process = invoke(["-q", "bad", "--api-url", url], tmp_path)
            assert process.returncode == 1
            assert "http_error" in process.stdout
            assert "Traceback" not in process.stderr
    saved = list(tmp_path.glob("results/*/results.json"))
    assert len(saved) == 2 and len(requests) == 2
    assert not list(tmp_path.glob("results/*/*.xlsx"))


def test_invalid_configuration_and_unwritable_output_send_nothing(sse_server, tmp_path):
    with sse_server() as (url, requests):
        process = invoke(["-q", "A", "-p", "0", "--api-url", url], tmp_path)
        assert process.returncode == 2
        assert not (tmp_path / "results").exists()
        (tmp_path / "results").write_text("a file blocks the output directory")
        process = invoke(["-q", "A", "--api-url", url], tmp_path)
        assert process.returncode == 1
        assert "output directory" in process.stderr.lower()
        assert "Traceback" not in process.stderr
        assert requests == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT subprocess integration")
def test_sigint_saves_completed_and_cancelled_requests(sse_server, tmp_path):
    with sse_server({"slow": (200, "text/event-stream", [
        (0, event("ai_message", {"content": "working"})),
        (5, event("ai_answer_end", {"analysis": "late"})),
    ])}) as (url, requests):
        process = subprocess.Popen([
            sys.executable, "-u", "-m", "holmes_benchmark", "--api-url", url,
            "-q", "fast", "-q", "slow", "-p", "2", "--export-to-excel",
        ], cwd=tmp_path, env=cli_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while len(requests) < 2 and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(requests) == 2, process.communicate(timeout=2)
            time.sleep(0.1)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 130, stderr
            assert "Interrupted" in stdout
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    path = next(tmp_path.glob("results/*/results.json"))
    report = json.loads(path.read_text())
    assert report["interrupted"]
    assert [r["outcome"] for r in report["requests"]] == ["success", "cancelled"]
    assert report["requests"][1]["raw_events"][0]["data"]["content"] == "working"
    assert path.with_name("report.xlsx").exists()


def test_excel_failure_preserves_json(sse_server, tmp_path, monkeypatch, capsys):
    from holmes_benchmark import cli

    def fail_export(*args):
        raise OSError("disk problem")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "write_excel", fail_export)
    with sse_server() as (url, _):
        code = cli.main(["--api-url", url, "-q", "A", "--export-to-excel"])
    assert code == 1
    assert len(list(tmp_path.glob("results/*/results.json"))) == 1
    assert "disk problem" in capsys.readouterr().err


def test_help_is_available_without_configuration(tmp_path):
    result = invoke(["--help"], tmp_path)
    assert result.returncode == 0
    assert "--export-to-excel" in result.stdout
    assert "HOLMES_API_URL" in result.stdout
    assert not list(tmp_path.iterdir())


def test_show_summary_includes_request_tokens_without_excel(sse_server, tmp_path):
    with sse_server({"A": (200, "text/event-stream", [
        (0, event("ai_answer_end", {"analysis": "done", "metadata": {"usage": {
            "prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168,
        }}})),
    ])}) as (url, _):
        process = invoke(["--api-url", url, "-q", "A", "--show-summary", "--no-progress"], tmp_path)
    assert process.returncode == 0, process.stderr
    assert process.stdout.startswith("\n")
    assert "HolmesGPT" in process.stdout and "Benchmarking" in process.stdout
    assert "Benchmark results" in process.stdout
    assert "123 / 45 / 168" in process.stdout
    assert "Last reported" in process.stdout and "not investigation totals" in process.stdout
    assert "Success: 1/1" in process.stdout
    assert "\x1b" not in process.stdout
    assert not list(tmp_path.glob("results/*/*.xlsx"))


def test_default_output_stays_brief(sse_server, tmp_path):
    with sse_server() as (url, _):
        process = invoke(["--api-url", url, "-q", "A", "--no-progress"], tmp_path)
    assert process.returncode == 0
    assert "Success: 1/1" in process.stdout
    assert "Benchmark results" not in process.stdout


def test_generated_timestamps_use_utc_plus_three_everywhere(sse_server, tmp_path):
    from datetime import datetime, timedelta
    import re
    from openpyxl import load_workbook

    env = cli_env()
    env["TZ"] = "UTC"  # Output must not inherit the host's timezone.
    with sse_server() as (url, _):
        process = invoke(["--api-url", url, "-q", "A", "--export-to-excel"], tmp_path, env)
    assert process.returncode == 0, process.stderr
    path = next(tmp_path.glob("results/*/results.json"))
    report = json.loads(path.read_text())
    timestamps = [report["started_at"], report["finished_at"]]
    for request in report["requests"]:
        timestamps.extend([request["started_at"], request["finished_at"]])
        timestamps.extend(event["received_at"] for event in request["raw_events"])
    assert all(datetime.fromisoformat(value).utcoffset() == timedelta(hours=3) for value in timestamps)
    assert report["settings"]["timezone"] == "UTC+3"
    stamp = path.parent.name.rsplit("-", 1)[0]
    assert stamp.endswith("+0300")
    directory_time = datetime.strptime(stamp, "%Y%m%dT%H%M%S%f%z")
    assert abs((datetime.fromisoformat(report["started_at"]) - directory_time).total_seconds()) < 10
    logs = re.findall(r"\[(\d\d:\d\d:\d\d) UTC\+3\]", process.stderr)
    assert logs
    start = datetime.fromisoformat(report["requests"][0]["started_at"])
    logged = datetime.combine(start.date(), datetime.strptime(logs[0], "%H:%M:%S").time(), start.tzinfo)
    assert abs((logged - start).total_seconds()) < 2
    book = load_workbook(path.with_name("report.xlsx"))
    metrics = {row[0].value: row[1].value for row in book["Summary"]}
    assert metrics["Run started (UTC+3)"] == report["started_at"]
    assert metrics["Finished (UTC+3)"] == report["finished_at"]
    book.close()


def test_live_terminal_updates_tools_and_restores_cursor(sse_server, tmp_path, monkeypatch):
    from io import StringIO
    from rich.console import Console
    from holmes_benchmark import cli, progress

    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=120, height=24)
    monkeypatch.setattr(progress, "Console", lambda **kwargs: console)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOLMES_API_TOKEN", raising=False)
    with sse_server({"A": (200, "text/event-stream", [
        (0, event("start_tool_calling", {"id": "a", "tool_name": "kubectl"})),
        (0.6, event("ai_answer_end", {"analysis": "done"})),
    ])}) as (url, _):
        code = cli.main(["--api-url", url, "-q", "A"])
    output = stream.getvalue()
    assert code == 0
    assert "Running: kubectl" in output
    assert "1/1 finished" in output
    assert "\x1b[?25l" in output and "\x1b[?25h" in output


def test_cli_shows_tool_activity_while_request_is_still_running(sse_server, tmp_path):
    with sse_server({"A": (200, "text/event-stream", [
        (0, event("start_tool_calling", {"id": "a", "tool_name": "kubectl"})),
        (1.2, event("tool_calling_result", {"tool_call_id": "a", "name": "kubectl",
                                            "result": {"status": "success"}})),
        (0, event("ai_answer_end", {"analysis": "done"})),
    ])}) as (url, _):
        proc = subprocess.Popen([sys.executable, "-m", "holmes_benchmark", "--api-url", url, "-q", "A"],
                                cwd=tmp_path, env=cli_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()

        def read_lines():
            for line in proc.stdout:
                lines.put(line)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        try:
            seen = ""
            deadline = time.monotonic() + 4
            while "Tool started: kubectl" not in seen and time.monotonic() < deadline:
                seen += lines.get(timeout=4)
            assert "Tool started: kubectl" in seen
            assert proc.poll() is None
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            reader.join(timeout=2)
            proc.stdout.close()


def test_no_progress_preserves_results_without_activity_logs(sse_server, tmp_path):
    with sse_server() as (url, _):
        result = invoke(["--api-url", url, "-q", "A", "--no-progress"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert "Success: 1/1" in result.stdout
