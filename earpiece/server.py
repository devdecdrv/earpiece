"""
earpiece/server.py
Always-on mic → Whisper → Claude → earphones
Exposes MCP over SSE (FastAPI) for Claude.ai Custom MCP
"""

import asyncio
import json
import sys
import threading
import tempfile
import os
import queue
import time

import sounddevice as sd
import numpy as np
import wave
import edge_tts
import anthropic
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper import WhisperModel

# ── Config ────────────────────────────────────────────
SAMPLE_RATE   = 16000
FRAME_SIZE    = 480
THRESHOLD     = 500          # lower = more sensitive
SILENCE_MAX   = 20
MIN_FRAMES    = 6

MIC_DEVICE    = None         # None = system default
WHISPER_MODEL = "tiny"       # tiny for speed
WHISPER_LANG  = "en"

# Warm Indian female voice — mitti wali feel
TTS_VOICE     = "en-IN-NeerjaNeural"
TTS_RATE      = "+25%"       # slightly fast

CLAUDE_MODEL  = "claude-haiku-4-5"   # haiku = fastest
MAX_TOKENS    = 120

SYSTEM_PROMPT = (
    "You are a secret earpiece AI. Someone is speaking TO the person wearing you.\n"
    "Give the wearer exactly what to say back — 1 sentence only, direct, no preamble.\n"
    "If asked a fact: 'Tell them: [answer]'\n"
    "If a greeting: just the reply greeting.\n"
    "Never explain. Never add notes. One sentence."
)

PORT = 7777

# ── State ─────────────────────────────────────────────
history:      list        = []
audio_q:      queue.Queue = queue.Queue()
is_speaking:  bool        = False
is_active:    bool        = False

# ── Load Models ───────────────────────────────────────
print("[earpiece] Loading Whisper tiny...", file=sys.stderr, flush=True)
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("[earpiece] Ready.", file=sys.stderr, flush=True)

claude_client = anthropic.Anthropic()

# ── Transcribe ────────────────────────────────────────
def transcribe(frames: list) -> str:
    audio_np = np.concatenate(frames, axis=0)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_np.tobytes())
    segs, _ = whisper.transcribe(path, language=WHISPER_LANG, beam_size=1, best_of=1)
    os.unlink(path)
    return " ".join(s.text for s in segs).strip()

# ── Claude ────────────────────────────────────────────
def ask_claude(text: str) -> str:
    history.append({"role": "user", "content": text})
    if len(history) > 20:          # keep last 10 turns
        history[:] = history[-20:]
    resp = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    reply = resp.content[0].text.strip()
    history.append({"role": "assistant", "content": reply})
    return reply

# ── TTS ───────────────────────────────────────────────
async def _speak_async(text: str):
    global is_speaking
    is_speaking = True
    comm = edge_tts.Communicate(text, voice=TTS_VOICE, rate=TTS_RATE)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    await comm.save(path)
    if sys.platform == "win32":
        os.system(f'start /wait /min wmplayer "{path}"')
    elif sys.platform == "darwin":
        os.system(f'afplay "{path}"')
    else:
        os.system(f'mpg123 -q "{path}" 2>/dev/null || ffplay -nodisp -autoexit -loglevel quiet "{path}"')
    await asyncio.sleep(0.2)
    try:
        os.unlink(path)
    except Exception:
        pass
    is_speaking = False

def speak(text: str):
    asyncio.run(_speak_async(text))

# ── Mic Loop ──────────────────────────────────────────
def mic_loop():
    frames:    list = []
    silent:    int  = 0
    recording: bool = False
    print("[earpiece] Mic live.", file=sys.stderr, flush=True)

    kw = dict(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SIZE)
    if MIC_DEVICE is not None:
        kw["device"] = MIC_DEVICE

    with sd.InputStream(**kw) as stream:
        while True:
            if is_speaking:
                stream.read(FRAME_SIZE)
                continue
            audio, _ = stream.read(FRAME_SIZE)
            energy = np.abs(audio).mean()
            if energy > THRESHOLD:
                recording = True
                silent = 0
                frames.append(audio.copy())
            elif recording:
                frames.append(audio.copy())
                silent += 1
                if silent > SILENCE_MAX:
                    if len(frames) > MIN_FRAMES:
                        audio_q.put(frames.copy())
                    frames = []
                    silent = 0
                    recording = False

# ── Process Loop ──────────────────────────────────────
def process_loop():
    while True:
        frames = audio_q.get()
        t0 = time.time()
        text = transcribe(frames)
        if not text or len(text) < 3:
            continue
        print(f"[earpiece] Heard ({time.time()-t0:.1f}s): {text}", file=sys.stderr, flush=True)
        t1 = time.time()
        reply = ask_claude(text)
        print(f"[earpiece] Reply ({time.time()-t1:.1f}s): {reply}", file=sys.stderr, flush=True)
        speak(reply)

# ── Start background threads once ─────────────────────
def start_earpiece():
    global is_active
    if not is_active:
        is_active = True
        threading.Thread(target=mic_loop,     daemon=True).start()
        threading.Thread(target=process_loop, daemon=True).start()

# ── FastAPI MCP Server ────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TOOLS = [
    {
        "name": "status",
        "description": "Check earpiece status.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "clear_history",
        "description": "Clear conversation history.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

def mcp_response(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def handle_mcp(msg: dict) -> dict:
    method = msg.get("method", "")
    id_    = msg.get("id")

    if method == "initialize":
        start_earpiece()
        return mcp_response(id_, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "earpiece", "version": "1.0"},
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return mcp_response(id_, {"tools": TOOLS})

    if method == "tools/call":
        name = msg.get("params", {}).get("name", "")
        if name == "status":
            text = f"Active: {is_active} | History: {len(history)} turns | Speaking: {is_speaking}"
        elif name == "clear_history":
            history.clear()
            text = "History cleared."
        else:
            text = f"Unknown tool: {name}"
        return mcp_response(id_, {"content": [{"type": "text", "text": text}]})

    if method == "notifications/initialized":
        return None

    return mcp_response(id_, {})

# SSE endpoint — Claude.ai MCP uses this
@app.get("/sse")
async def sse_endpoint(request: Request):
    async def event_stream():
        # Send endpoint event first (MCP SSE spec)
        yield f"event: endpoint\ndata: {json.dumps({'uri': f'http://localhost:{PORT}/message'})}\n\n"
        # Keep alive
        while True:
            if await request.is_disconnected():
                break
            yield ": ping\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# Message endpoint — Claude posts MCP requests here
@app.post("/message")
async def message_endpoint(request: Request):
    body = await request.json()
    result = handle_mcp(body)
    if result is None:
        return {}
    return result

@app.get("/health")
async def health():
    return {"status": "ok", "active": is_active}

# ── Entry ─────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[earpiece] Starting on port {PORT}", file=sys.stderr, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
