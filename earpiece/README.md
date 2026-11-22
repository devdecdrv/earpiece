# earpiece 🎧 — Jarvis for your ears

No API key. No internet. Fully local.

## Setup

```bash
pip install -r requirements.txt
python earpiece.py
```

## Requirements

- Ollama running with llama3.2:3b
- Microphone
- Earphones

## Tune

Edit top of `earpiece.py`:

| Setting | Default | Notes |
|---------|---------|-------|
| `THRESHOLD` | 500 | Mic sensitivity — lower = picks up more |
| `TTS_VOICE` | `en-IN-NeerjaNeural` | Neerja — warm Indian female voice |
| `TTS_RATE` | `+25%` | Speed |
| `WHISPER_MODEL` | `tiny` | `small` = more accurate |
| `MIC_DEVICE` | `None` | Set int index for specific mic |
