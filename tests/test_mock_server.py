"""Exercise the standalone demo server with the installed CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_standalone_mock_with_actual_cli(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "mock_holmes.py"
    server = subprocess.Popen([sys.executable, "-u", str(script), "--port", "0"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        banner = server.stdout.readline().strip()
        assert banner.startswith("Mock HolmesGPT: http://127.0.0.1:"), banner
        url = banner.split()[-1]
        env = {key: value for key, value in os.environ.items() if not key.startswith("HOLMES_")}
        cli = Path(sys.executable).with_name("holmes-benchmark")
        result = subprocess.run([str(cli), "--api-url", url, "-p", "5",
                                 "-q", "Check pods", "-q", "Check nodes", "--export-to-excel"],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "Success: 5/5" in result.stdout
        path = next(tmp_path.glob("results/*/results.json"))
        report = json.loads(path.read_text())
        assert [r["question"] for r in report["requests"]] == [
            "Check pods", "Check nodes", "Check pods", "Check nodes", "Check pods",
        ]
        assert all(r["answer"] == "Mock response: all pods and nodes are healthy."
                   for r in report["requests"])
        assert all(r["completion_seconds"] - r["first_text_seconds"] >= 0.7
                   for r in report["requests"])
        assert report["summary"]["batch_duration_seconds"] < 4
        assert path.with_name("report.xlsx").exists()
    finally:
        server.terminate()
        server.communicate(timeout=5)
