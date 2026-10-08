# J.A.R.V.I.S. — Voice-Controlled Smart Lights

An always-on voice assistant for Govee smart bulbs. Say *"Jarvis, make the living room blue"* or *"Jarvis, something cozy for movie night"* and the lights respond.

Speech recognition runs **fully offline**, and simple commands never touch an LLM. Only ambiguous, natural-language requests are escalated to Claude, which keeps the system fast, private, and close to free to run.

## How it works

```
 microphone ──► Vosk (offline STT) ──► wake-word detector ──► command text
                                                                  │
                                         ┌────────────────────────┴───────────────┐
                                         ▼                                        ▼
                               Fast path (local parser)               Claude (warm CLI session)
                           "lights off", "brightness fifty"        "something cozy for movie night"
                                  ~instant, no tokens                  → JSON array of actions
                                         └────────────────────────┬───────────────┘
                                                                  ▼
                                                  validate + clamp actions
                                                                  ▼
                                                    Govee Cloud API ──► bulbs
```

**Design decisions**

- **Two-tier interpretation.** A hand-written parser handles common phrases (on/off, colors, brightness in spoken numbers like "seventy five", scenes, room targeting). Anything it can't parse falls through to an LLM, which returns a strict JSON action list.
- **Warm LLM session with recycling.** Instead of spawning a new process per request, Jarvis keeps one streaming `claude` process alive and restarts it every few commands so context stays small and latency stays low.
- **Untrusted model output.** Every LLM action is validated against an allowlist and numerically clamped (RGB 0–255, color temp 2000–9000K) before it reaches hardware.
- **Fuzzy wake word.** Speech models mishear names, so the wake word matches by string similarity ("jervis", "jarvus", and a split "jar vis" all count), with a follow-up window after a bare "Jarvis".
- **Auto gain** normalizes quiet microphones before recognition.
- **Zero-dependency UI.** An animated arc-reactor terminal UI with a live mic waveform and ASCII bulbs, built with pure ANSI truecolor escapes.
- **Stdlib-only API client.** The Govee wrapper uses only `urllib`, and the device list is cached for 10 minutes to cut API round-trips.

## Project layout

| File | Purpose |
|---|---|
| `voice.py` | Always-on listener: mic capture, auto gain, Vosk STT, wake-word detection |
| `jarvis.py` | Command interpretation (fast path + Claude), validation, execution |
| `govee.py` | Govee Cloud API wrapper; also usable as a standalone CLI |
| `ui.py` | Animated terminal UI (`python ui.py --demo` to preview) |
| `run-jarvis.bat` | Windows launcher that auto-restarts the listener |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then add your Govee API key
```

Download a Vosk English model (e.g. `vosk-model-small-en-us-0.15`) from https://alphacephei.com/vosk/models into `models/`. The [Claude Code CLI](https://claude.com/claude-code) must be installed and logged in for the LLM path.

## Usage

```bash
python voice.py                  # always-on voice mode with the animated UI
python voice.py --dry            # show what would be sent, without touching the lights
python voice.py --wav test.wav   # run a 16 kHz mono recording through the pipeline
python jarvis.py "rainbow in the living room"   # one-off text command
python govee.py devices          # list bulbs
```

## Tech

Python · Vosk (Kaldi) · sounddevice · Govee Cloud API · Claude (Haiku)
