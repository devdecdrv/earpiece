"""
earpiece.py — Jarvis for your ears
Mic → Whisper → Ollama (llama3.2:3b) → Neerja voice → earphones
No API key. No internet. Fully local.
"""

import sys
import threading
import tempfile
import os
import queue
import time
import urllib.request
import json

import sounddevice as sd
import numpy as np
import wave
import asyncio
import edge_tts
from faster_whisper import WhisperModel

# ── Config ────────────────────────────────────────────
SAMPLE_RATE   = 16000
FRAME_SIZE    = 480
THRESHOLD     = 500       # mic sensitivity (lower = more sensitive)
SILENCE_MAX   = 20        # silent frames before cut
MIN_FRAMES    = 6         # minimum frames to process

MIC_DEVICE    = None      # None = system default mic

WHISPER_MODEL = "tiny"    # tiny = fastest
WHISPER_LANG  = "en"

OLLAMA_URL    = "http://localhost:11434/api/generate"
OLLAMA_MODEL  = "llama3.2:3b"

TTS_VOICE     = "en-IN-NeerjaNeural"   # warm Indian female voice
TTS_RATE      = "+25%"

SYSTEM_PROMPT = (
    "You are a secret earpiece AI assistant named Jarvis. "
    "Someone is speaking TO the person wearing you. "
    "Your job: tell the wearer exactly what to say back in 1 sentence only. "
    "Be direct. No explanation. No preamble. No notes. "
    "Just the reply they should speak. "
    "Example — heard: 'what is photosynthesis' → reply: 'Tell them: plants convert sunlight into food using chlorophyll.' "
    "Example — heard: 'how are you' → reply: 'I am doing great, thank you for asking.' "
)

# ── State ─────────────────────────────────────────────
history:     list         = []
audio_q:     queue.Queue  = queue.Queue()
is_speaking: bool         = False

# ── Load Whisper ──────────────────────────────────────
print("[earpiece] Loading Whisper...", flush=True)
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("[earpiece] Ready. Mic is live.\n", flush=True)

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

# ── Ollama ────────────────────────────────────────────
def ask_ollama(text: str) -> str:
    history.append({"role": "user", "content": text})
    if len(history) > 10:
        history[:] = history[-10:]

    # Build prompt
    prompt = SYSTEM_PROMPT + "\n\n"
    for turn in history:
        role = "Human" if turn["role"] == "user" else "Jarvis"
        prompt += f"{role}: {turn['content']}\n"
    prompt += "Jarvis:"

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 80,
        }
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    reply = data.get("response", "").strip()

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

        print(f"Heard  : {text}", flush=True)

        try:
            reply = ask_ollama(text)
        except Exception as e:
            print(f"Ollama error: {e}", flush=True)
            continue

        print(f"Reply  : {reply}", flush=True)
        print(f"Time   : {time.time()-t0:.1f}s\n", flush=True)

        speak(reply)

# ── Main ──────────────────────────────────────────────
def main():
    print("=" * 50)
    print("  EARPIECE — Jarvis mode")
    print("  Listening... speak near mic")
    print("=" * 50 + "\n")

    threading.Thread(target=mic_loop,     daemon=True).start()
    threading.Thread(target=process_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[earpiece] Stopped.")

if __name__ == "__main__":
    main()
