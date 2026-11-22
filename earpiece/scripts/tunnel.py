"""
tunnel.py — Cloudflare tunnel launcher for earpiece MCP

What this does:
  1. Runs `cloudflared tunnel --url http://localhost:PORT`
  2. Parses the generated *.trycloudflare.com URL from stdout
  3. Prints a ready-to-paste Claude MCP config block
  4. Keeps the tunnel alive until Ctrl-C

Requirements:
  - cloudflared installed and on PATH
      Windows : winget install Cloudflare.cloudflared
      Linux   : apt install cloudflared  OR  download binary
      macOS   : brew install cloudflared

Usage:
  python scripts/tunnel.py              # default port 8000
  python scripts/tunnel.py --port 9000
"""

import subprocess
import sys
import re
import argparse
import threading
import time

def find_tunnel_url(proc: subprocess.Popen) -> str | None:
    """Read cloudflared stderr until we see the tunnel URL."""
    pattern = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')
    for line in proc.stderr:
        line_s = line.decode(errors="replace").strip()
        print(f"[cloudflared] {line_s}", file=sys.stderr)
        m = pattern.search(line_s)
        if m:
            return m.group(0)
    return None

def forward_stderr(proc: subprocess.Popen):
    """Keep printing cloudflared stderr after URL found."""
    for line in proc.stderr:
        print(f"[cloudflared] {line.decode(errors='replace').strip()}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="Cloudflare tunnel for earpiece MCP")
    parser.add_argument("--port", type=int, default=8000, help="Local port the MCP HTTP bridge listens on")
    args = parser.parse_args()

    port = args.port
    print(f"[tunnel] Starting cloudflared → localhost:{port}", file=sys.stderr)

    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    url = find_tunnel_url(proc)
    if not url:
        print("[tunnel] ERROR: Could not parse tunnel URL. Is cloudflared installed?", file=sys.stderr)
        proc.terminate()
        sys.exit(1)

    # Keep printing remaining cloudflared output in background
    t = threading.Thread(target=forward_stderr, args=(proc,), daemon=True)
    t.start()

    # Print the config block clearly
    config = f"""
╔══════════════════════════════════════════════════════════════╗
║  EARPIECE TUNNEL READY                                       ║
╠══════════════════════════════════════════════════════════════╣
║  URL : {url:<53}║
╚══════════════════════════════════════════════════════════════╝

Paste this into Claude → Settings → Custom MCP servers:

{{
  "mcpServers": {{
    "earpiece": {{
      "command": "npx",
      "args": [
        "mcp-remote",
        "{url}"
      ]
    }}
  }}
}}

Press Ctrl-C to stop the tunnel.
"""
    print(config)

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[tunnel] Stopping...", file=sys.stderr)
        proc.terminate()

if __name__ == "__main__":
    main()
