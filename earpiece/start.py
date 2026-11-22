"""
start.py — launches server.py + cloudflared tunnel, prints MCP URL
"""

import subprocess
import sys
import re
import time
import threading
import signal
import urllib.request
import os
from pathlib import Path

PORT = 7777
ROOT = Path(__file__).resolve().parent

procs: list = []

def fwd(stream, tag):
    for line in stream:
        s = line.decode(errors="replace").rstrip()
        if s:
            print(f"[{tag}] {s}", file=sys.stderr, flush=True)

def wait_up(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.4)
    return False

def find_url(proc) -> str | None:
    pat = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')
    for line in proc.stderr:
        s = line.decode(errors="replace").strip()
        print(f"[cloudflared] {s}", file=sys.stderr, flush=True)
        m = pat.search(s)
        if m:
            return m.group(0)
    return None

def cleanup(*_):
    print("\n[earpiece] Stopping...", file=sys.stderr)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT,  cleanup)
signal.signal(signal.SIGTERM, cleanup)

# ── 1. Server ─────────────────────────────────────────
print("[start] Launching earpiece server...", file=sys.stderr, flush=True)

# When running as .exe (PyInstaller), sys.executable is the exe itself
if getattr(sys, "frozen", False):
    # We ARE the exe — server runs in-process, not as subprocess
    # Import and run server directly
    import server
    t = threading.Thread(
        target=lambda: __import__("uvicorn").run(
            server.app, host="0.0.0.0", port=PORT, log_level="warning"
        ),
        daemon=True,
    )
    t.start()
else:
    srv = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    procs.append(srv)
    threading.Thread(target=fwd, args=(srv.stderr, "server"), daemon=True).start()

if not wait_up(PORT):
    print("[start] ERROR: Server did not start.", file=sys.stderr)
    cleanup()

print("[start] Server up.", file=sys.stderr, flush=True)

# ── 2. Cloudflared ────────────────────────────────────
print("[start] Starting Cloudflare tunnel...", file=sys.stderr, flush=True)

tun = subprocess.Popen(
    ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
)
procs.append(tun)
url = find_url(tun)

if not url:
    print("[start] ERROR: Tunnel URL not found. Install cloudflared and retry.", file=sys.stderr)
    cleanup()

threading.Thread(target=fwd, args=(tun.stderr, "cloudflared"), daemon=True).start()

# ── 3. Print config ───────────────────────────────────
config_json = f'{{"mcpServers":{{"earpiece":{{"command":"npx","args":["mcp-remote","{url}/sse"]}}}}}}'

banner = f"""
╔══════════════════════════════════════════════════════════════════╗
║  EARPIECE READY 🎧                                              ║
╠══════════════════════════════════════════════════════════════════╣
║  {url:<63}║
╚══════════════════════════════════════════════════════════════════╝

Claude.ai → Settings → Custom MCP → Edit config → paste this:

{config_json}

Mic is live. Ctrl-C to stop.
"""
print(banner)

# ── 4. Keep alive ─────────────────────────────────────
try:
    tun.wait()
except KeyboardInterrupt:
    cleanup()
