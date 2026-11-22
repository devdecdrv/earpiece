"""
http_bridge.py — HTTP ↔ stdio bridge for earpiece MCP

Cloudflare tunnel exposes HTTP. Claude's MCP client speaks HTTP/SSE.
This bridge:
  - Listens on localhost:PORT
  - Spawns src/server.py as a subprocess (stdio MCP)
  - Forwards HTTP POST /mcp → stdin, stdout → HTTP response
  - Handles SSE for streaming if needed

Usage:
  python scripts/http_bridge.py              # port 8000
  python scripts/http_bridge.py --port 9000

Then in another terminal:
  python scripts/tunnel.py --port 8000
"""

import argparse
import subprocess
import sys
import json
import threading
import queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Shared subprocess handle
_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_resp_q: queue.Queue = queue.Queue()

def get_proc() -> subprocess.Popen:
    global _proc
    with _proc_lock:
        if _proc is None or _proc.poll() is not None:
            _proc = subprocess.Popen(
                [sys.executable, str(ROOT / "src" / "server.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                bufsize=0,
            )
            # Reader thread
            t = threading.Thread(target=_read_stdout, args=(_proc,), daemon=True)
            t.start()
    return _proc

def _read_stdout(proc: subprocess.Popen):
    for raw in proc.stdout:
        raw = raw.strip()
        if raw:
            _resp_q.put(raw)

def send_to_server(msg: dict) -> dict:
    proc   = get_proc()
    line   = (json.dumps(msg) + "\n").encode()
    with _proc_lock:
        proc.stdin.write(line)
        proc.stdin.flush()
    # Wait for response (5 s timeout)
    try:
        raw  = _resp_q.get(timeout=5)
        return json.loads(raw)
    except queue.Empty:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": "timeout"}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(e)}}

class MCPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # silence default access log

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "server": "earpiece"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            msg  = json.loads(body)
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        resp = send_to_server(msg)
        self._json(200, resp)

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Pre-warm the subprocess
    get_proc()

    server = HTTPServer(("localhost", args.port), MCPHandler)
    print(f"[bridge] Listening on http://localhost:{args.port}", file=sys.stderr)
    print(f"[bridge] MCP server subprocess started.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] Stopping.", file=sys.stderr)

if __name__ == "__main__":
    main()
