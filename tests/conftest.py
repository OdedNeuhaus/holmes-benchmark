"""A real local HTTP server with controllable streamed responses."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest


def event(name, data):
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@contextmanager
def serve(plans=None, barrier=1):
    plans = plans or {}
    requests = []
    lock = threading.Lock()
    ready = threading.Event()
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                requests.append({"body": payload, "headers": dict(self.headers),
                                 "at": time.monotonic(), "path": self.path})
                if len(requests) >= barrier:
                    ready.set()
            ready.wait(2)
            status, content_type, chunks = plans.get(payload["ask"], (
                200, "text/event-stream", [(0, event("ai_answer_end", {"analysis": "done"}))]))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for delay, chunk in chunks:
                    if stop.wait(delay):
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/chat", requests
    finally:
        stop.set()
        ready.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def sse_server():
    return serve
