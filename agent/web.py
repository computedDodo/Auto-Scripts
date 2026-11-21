#!/usr/bin/env python3
"""Loopback-only, read-only browser UI for the Termux File Agent.

The browser can search and preview files. Editing, script execution, AI tools,
and Git operations remain available only through the original terminal agent.
"""
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HTML_FILE = HERE / "index.html"
CORE_FILE = ROOT / "Termux_file-agent_v11.0.1.py"
HOST = "127.0.0.1"
PORT = 8765
MAX_PREVIEW_CHARS = 30_000

spec = importlib.util.spec_from_file_location("termux_file_agent_core", CORE_FILE)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load original agent: {CORE_FILE}")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def allowed_file(path):
    """Only permit previewing files under the agent's indexed storage roots."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        roots = [Path(core.HOME_DIR).resolve(), Path(core.SHARED_DIR).resolve()]
        return resolved.is_file() and any(os.path.commonpath((str(root), str(resolved))) == str(root) for root in roots), resolved
    except (OSError, ValueError):
        return False, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        # Avoid writing search terms or local file paths into server logs.
        pass

    def _send(self, status, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        # Binding to loopback is not enough on its own; reject unexpected Host headers.
        if self.headers.get("Host", "").lower() not in {f"{HOST}:{PORT}", f"localhost:{PORT}"}:
            self._json(403, {"error": "Host not allowed"})
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/":
            try:
                self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._json(500, {"error": "Web page could not be loaded"})
            return

        if parsed.path == "/api/search":
            keyword = params.get("q", [""])[0].strip()
            if not keyword:
                self._json(400, {"error": "Enter a search term"})
                return
            try:
                matches = core.search_files(keyword)[:50]
                self._json(200, {"results": matches})
            except Exception:
                self._json(500, {"error": "Search failed; check the Termux console"})
            return

        if parsed.path == "/api/read":
            requested = unquote(params.get("path", [""])[0])
            permitted, resolved = allowed_file(requested)
            if not permitted:
                self._json(403, {"error": "That file is outside the indexed storage roots or unavailable"})
                return
            try:
                content = core.read_file(str(resolved), silent=True)
                truncated = len(content) > MAX_PREVIEW_CHARS
                self._json(200, {
                    "path": str(resolved),
                    "content": content[:MAX_PREVIEW_CHARS],
                    "truncated": truncated,
                })
            except Exception:
                self._json(500, {"error": "File could not be previewed"})
            return

        self._json(404, {"error": "Not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Read-only File Agent UI: http://{HOST}:{PORT}")
    print("Only this device can connect. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
