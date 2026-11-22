"""
earpiece — always-on mic → Claude → earphones
MCP server via stdio (Cloudflare tunnel wraps HTTP → stdio bridge)
"""

import asyncio
import json
import sys
import threading
import tempfile
import os
import queue

import sounddevice as sd
import numpy as np
import wave
import edge_tts
import anthropic
from faster_whisper import WhisperModel

# ── Config (edit these) ───────────────────────────────
SAMPLE_RATE   = 16000
FRAME_SIZE    = 480
THRESHOLD     = 600          # energy threshold to start recording
SILENCE_MAX   = 25           # silent frames before cut
MIN_FRAMES    = 8            # minimum frames to transcribe

MIC_DEVICE    = None         # None = system default, or set int device index
OUT_DEVICE    = None         # None = system default

TTS_VOICE     = "en-US-AriaNeural"
TTS_RATE      = "+20%"

WHISPER_MODEL = "small"      # tiny / base / small / medium
WHISPER_LANG  = "en"         # or "hi" for Hindi, "auto" for auto-detect

CLAUDE_MODEL  = "claude-sonnet-4-6"
MAX_TOKENS    = 150

SYSTEM_PROMPT = (
    "You are a secret earpiece AI assistant. "
    "Someone is talking TO the person wearing you.\n"
    "Your job: tell the wearer exactly what to say back — in 1–2 short sentences.\n"
    "Be direct. No explanation. No preamble. Just the reply they should give.\n"
    "Example: if someone asks 'what is photosynthesis' — reply: "
    "'Tell them: plants convert sunlight into food using chlorophyll in their cells.'"
)

# ── State ─────────────────────────────────────────────
history:    list  = []
audio_q:    queue.Queue = queue.Queue()
is_speaking: bool = False
is_active:   bool = False   # set True after MCP initialize

# ── Load Models ───────────────────────────────────────
print("[earpiece] Loading Whisper...", file=sys.stderr, flush=True)
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("[earpiece] Whisper ready.", file=sys.stderr, flush=True)

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
    segments, _ = whisper.transcribe(path, language=WHISPER_LANG)
    os.unlink(path)
    return " ".join(s.text for s in segments).strip()

# ── Claude ────────────────────────────────────────────
def ask_claude(text: str) -> str:
    history.append({"role": "user", "content": text})
    resp = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    reply = resp.content[0].text
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
    # Cross-platform playback
    if sys.platform == "win32":
        os.system(f'start /wait /min wmplayer "{path}"')
    elif sys.platform == "darwin":
        os.system(f'afplay "{path}"')
    else:
        os.system(f'mpg123 -q "{path}" 2>/dev/null || ffplay -nodisp -autoexit "{path}" 2>/dev/null')
    await asyncio.sleep(0.3)
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

    print("[earpiece] Mic loop started.", file=sys.stderr, flush=True)

    stream_kwargs = dict(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=FRAME_SIZE,
    )
    if MIC_DEVICE is not None:
        stream_kwargs["device"] = MIC_DEVICE

    with sd.InputStream(**stream_kwargs) as stream:
        while True:
            if is_speaking:
                stream.read(FRAME_SIZE)   # drain to avoid latency
                continue

            audio, _ = stream.read(FRAME_SIZE)
            energy    = np.abs(audio).mean()

            if energy > THRESHOLD:
                recording = True
                silent    = 0
                frames.append(audio.copy())
            elif recording:
                frames.append(audio.copy())
                silent += 1
                if silent > SILENCE_MAX:
                    if len(frames) > MIN_FRAMES:
                        audio_q.put(frames.copy())
                    frames    = []
                    silent    = 0
                    recording = False

# ── Process Loop ──────────────────────────────────────
def process_loop():
    while True:
        frames = audio_q.get()
        text   = transcribe(frames)
        if not text or len(text) < 4:
            continue
        print(f"[earpiece] Heard: {text}", file=sys.stderr, flush=True)
        reply = ask_claude(text)
        print(f"[earpiece] Reply: {reply}", file=sys.stderr, flush=True)
        speak(reply)

# ── MCP stdio protocol ────────────────────────────────
def send(obj: dict):
    print(json.dumps(obj), flush=True)

def handle(msg: dict):
    global is_active
    method = msg.get("method", "")
    mid    = msg.get("id")

    if method == "initialize":
        if not is_active:
            is_active = True
            threading.Thread(target=mic_loop,     daemon=True).start()
            threading.Thread(target=process_loop, daemon=True).start()
        print("[earpiece] Active — listening.", file=sys.stderr, flush=True)
        send({
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "earpiece", "version": "3.0"},
                "capabilities": {"tools": {}},
            },
        })

    elif method == "notifications/initialized":
        pass   # ack only

    elif method == "tools/list":
        send({
            "jsonrpc": "2.0", "id": mid,
            "result": {"tools": [
                {
                    "name": "status",
                    "description": "Returns earpiece status and conversation history length.",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                },
                {
                    "name": "clear_history",
                    "description": "Wipes conversation history so Claude starts fresh.",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                },
                {
                    "name": "set_threshold",
                    "description": "Adjust mic sensitivity threshold (default 600).",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "number", "description": "New threshold (100–5000)"}
                        },
                        "required": ["value"],
                    },
                },
            ]},
        })

    elif method == "tools/call":
        name   = msg.get("params", {}).get("name", "")
        args   = msg.get("params", {}).get("arguments", {})
        result = _tool_call(name, args)
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": result}]}})

    else:
        # Unknown method — send empty ok
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})

def _tool_call(name: str, args: dict) -> str:
    global THRESHOLD
    if name == "status":
        return (
            f"Earpiece {'active' if is_active else 'idle'} | "
            f"History turns: {len(history)} | "
            f"Threshold: {THRESHOLD} | "
            f"Speaking: {is_speaking}"
        )
    if name == "clear_history":
        history.clear()
        return "History cleared."
    if name == "set_threshold":
        val = int(args.get("value", THRESHOLD))
        val = max(100, min(5000, val))
        THRESHOLD = val
        return f"Threshold set to {THRESHOLD}."
    return f"Unknown tool: {name}"

# ── Entry ─────────────────────────────────────────────
def main():
    print("[earpiece] Server started. Waiting for MCP init...", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            handle(msg)
        except Exception as e:
            print(f"[earpiece] Error: {e}", file=sys.stderr, flush=True)

if __name__ == "__main__":
    main()
