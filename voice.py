"""Always-on wake-word listener. Local speech-to-text (Vosk) runs continuously and
costs nothing; only text said after "jarvis" / "hey jarvis" is sent to Jarvis.

    .venv/bin/python voice.py                 # listen on the default microphone
    .venv/bin/python voice.py --dry           # print what would be sent, don't act
    .venv/bin/python voice.py --wav test.wav  # run a 16 kHz mono wav through the pipeline
"""
import json
import os
import queue
import sys
import time
import wave

from vosk import KaldiRecognizer, Model, SetLogLevel

import govee
import jarvis

MODEL_DIR = os.environ.get(
    "VOSK_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "vosk-model-small-en-us-0.15"),
)
RATE = 16000
WAKE_WORDS = {"jarvis", "jarvus", "jarvas"}
FOLLOWUP_SECONDS = 8  # after a bare "jarvis", how long we wait for the command


class Listener:
    def __init__(self, dry=False):
        self.dry = dry
        self.awaiting_until = 0.0

    def on_utterance(self, text):
        """Called with each finished utterance. Returns True if a command was dispatched."""
        words = text.lower().split()
        if not words:
            return False
        idx = next((i for i, w in enumerate(words) if w in WAKE_WORDS), None)
        if idx is not None:
            command = " ".join(words[idx + 1:])
            if not command:
                print("[jarvis] listening...")
                self.awaiting_until = time.time() + FOLLOWUP_SECONDS
                return False
        elif time.time() < self.awaiting_until:
            command = " ".join(words)
        else:
            return False  # not addressed to us; ignore, costs nothing
        self.awaiting_until = 0.0
        print(f"[heard] {command}")
        if not self.dry:
            jarvis.handle(command)
        return True


def listen_mic(listener, recognizer):
    import sounddevice as sd

    audio = queue.Queue()

    def callback(indata, frames, t, status):
        audio.put(bytes(indata))

    print("Listening for 'Jarvis'... (Ctrl-C to stop)")
    with sd.RawInputStream(samplerate=RATE, blocksize=4000, dtype="int16",
                           channels=1, callback=callback):
        while True:
            if recognizer.AcceptWaveform(audio.get()):
                text = json.loads(recognizer.Result())["text"]
                if listener.on_utterance(text):
                    # Drop audio buffered while the command was running.
                    while not audio.empty():
                        audio.get_nowait()
                    recognizer.Reset()


def listen_wav(listener, recognizer, path):
    with wave.open(path) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1, "need 16 kHz mono wav"
        while True:
            data = w.readframes(4000)
            if not data:
                break
            if recognizer.AcceptWaveform(data):
                listener.on_utterance(json.loads(recognizer.Result())["text"])
    listener.on_utterance(json.loads(recognizer.FinalResult())["text"])


def main(argv):
    govee.load_env()
    SetLogLevel(-1)
    if not os.path.isdir(MODEL_DIR):
        raise SystemExit(f"Vosk model not found at {MODEL_DIR}")
    recognizer = KaldiRecognizer(Model(MODEL_DIR), RATE)
    listener = Listener(dry="--dry" in argv)
    if "--wav" in argv:
        listen_wav(listener, recognizer, argv[argv.index("--wav") + 1])
        return
    try:
        listen_mic(listener, recognizer)
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main(sys.argv)
