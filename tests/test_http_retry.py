import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from oex.hdx_publisher import _download_json


def test_a_transient_server_error_is_retried() -> None:
    """A 503 followed by a 200 returns the 200 body."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append(self.path)
            if len(calls) == 1:
                self.send_response(503)
                self.end_headers()
                return
            body = json.dumps({"type": "Collection"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/metadata.json"
        assert _download_json(url) == {"type": "Collection"}
    finally:
        server.shutdown()
    assert len(calls) == 2
