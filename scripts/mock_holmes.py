#!/usr/bin/env python3
"""Local demo server: fixed HolmesGPT SSE responses, no model or cluster calls."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time


class MockHolmes(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404, "Use /api/chat")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not isinstance(body, dict) or not isinstance(body.get("ask"), str):
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_error(400, "Expected a JSON object with an ask string")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            time.sleep(0.2)
            self.event("ai_message", {"content": "Checking the mock cluster..."})
            time.sleep(0.8)
            self.event("ai_answer_end", {
                "analysis": "Mock response: all pods and nodes are healthy.",
                "metadata": {"usage": {
                    "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                }},
            })
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected if the benchmark times out or is interrupted.

    def event(self, name, data):
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081, help="localhost port (default: 8081)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), MockHolmes) as server:
        print(f"Mock HolmesGPT: http://127.0.0.1:{server.server_port}/api/chat", flush=True)
        print("Fixed answers, simulated token counts, ~1s per request. Ctrl-C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nMock server stopped.")


if __name__ == "__main__":
    main()
