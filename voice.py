"""Always-on wake-word listener. Local speech-to-text (Vosk) runs continuously and
costs nothing; only text said after "jarvis" / "hey jarvis" is sent to Jarvis.

    .venv/bin/python voice.py                 # listen on the default microphone
    .venv/bin/python voice.py --dry           # print what would be sent, don't act
    .venv/bin/python voice.py --wav test.wav  # run a 16 kHz mono wav through the pipeline
"""
import difflib
import json
import os
import queue
import sys
import traceback
from array import array
import time
import wave

from vosk import KaldiRecognizer, Model, SetLogLevel

import govee
import jarvis

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def pick_model():
    """VOSK_MODEL if set, else the most accurate model that is installed."""
    if os.environ.get("VOSK_MODEL"):
        return os.environ["VOSK_MODEL"]
    for name in ("vosk-model-en-us-0.22-lgraph", "vosk-model-small-en-us-0.15"):
        if os.path.isdir(os.path.join(MODELS_DIR, name)):
            return os.path.join(MODELS_DIR, name)
    return os.path.join(MODELS_DIR, "vosk-model-small-en-us-0.15")


MODEL_DIR = pick_model()
RATE = 16000
WAKE_WORDS = {"jarvis", "jarvus", "jarvas"}
WAKE_SIMILARITY = 0.66  # "travis", "jervis", "jarvus"... count as the wake word
GAIN = float(os.environ.get("JARVIS_GAIN", "0"))  # 0 = automatic gain; set e.g. 8 for a fixed boost
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis.log")
FOLLOWUP_SECONDS = 8  # after a bare "jarvis", how long we wait for the command


def is_wake(word):
    return word in WAKE_WORDS or difflib.SequenceMatcher(None, word, "jarvis").ratio() >= WAKE_SIMILARITY


def find_wake(words):
    """Index of the last word of the wake word ("jarvis" or split "jar vis"), else None."""
    for i, w in enumerate(words):
        if is_wake(w):
            return i
        if i + 1 < len(words) and is_wake(w + words[i + 1]):
            return i + 1
    return None


class Listener:
    def __init__(self, dry=False, ui=None):
        self.dry = dry
        self.ui = ui
        self.awaiting_until = 0.0

    def log(self, msg):
        if self.ui:
            self.ui.log(msg)
        else:
            print(msg)

    def on_utterance(self, text, peak=None):
        """Called with each finished utterance. Returns True if a command was dispatched."""
        words = text.lower().split()
        if not words:
            return False
        idx = find_wake(words)
        if idx is not None:
            command = " ".join(words[idx + 1:])
            if not command:
                self.log("[jarvis] listening...")
                if self.ui:
                    self.ui.set_state("wake", FOLLOWUP_SECONDS)
                self.awaiting_until = time.time() + FOLLOWUP_SECONDS
                return False
        elif time.time() < self.awaiting_until:
            command = " ".join(words)
        else:
            # Not addressed to us: ignore (costs nothing), but log it to help tune recognition.
            self.log(f"~ {text}" + (f" (peak {peak})" if peak is not None else ""))
            return False
        self.awaiting_until = 0.0
        self.log(f"[heard] {command}")
        if self.ui:
            self.ui.heard = command
            self.ui.count += 1
            self.ui.set_state("heard")
        if not self.dry:
            jarvis.handle(command)
        return True


class AutoGain:
    """Scales quiet mics up toward a target level, tracking the loudest recent audio.
    Fast attack (current chunk never exceeds the target), slow release (~7 s half-life)."""
    TARGET, FLOOR, MAX_GAIN, DECAY = 12000, 500, 40.0, 0.99

    def __init__(self):
        self.loud = self.FLOOR

    def gain_for(self, raw_peak):
        self.loud = max(self.loud * self.DECAY, raw_peak, self.FLOOR)
        return min(self.MAX_GAIN, self.TARGET / self.loud)


_agc = AutoGain()


def amplify(chunk):
    """Apply gain to 16-bit PCM with clipping; return (bytes, peak after gain).
    JARVIS_GAIN=<n> forces a fixed gain instead of automatic."""
    samples = array("h")
    samples.frombytes(chunk)
    raw_peak = max(map(abs, samples), default=0)
    gain = GAIN if GAIN else _agc.gain_for(raw_peak)
    if gain != 1:
        samples = array("h", (max(-32768, min(32767, int(s * gain))) for s in samples))
    return samples.tobytes(), min(32768, int(raw_peak * gain))


def listen_mic(listener, recognizer):
    import sounddevice as sd

    ui = listener.ui
    audio = queue.Queue()

    def callback(indata, frames, t, status):
        audio.put(bytes(indata))

    listener.log("Listening for 'Jarvis'...")
    if ui:
        ui.set_state("idle")
    peak = 0
    with sd.RawInputStream(samplerate=RATE, blocksize=1600, dtype="int16",
                           channels=1, callback=callback):
        while True:
            chunk, chunk_peak = amplify(audio.get())
            peak = max(peak, chunk_peak)
            if ui:
                ui.push_level((chunk_peak / 20000) ** 0.5)
            if recognizer.AcceptWaveform(chunk):
                text = json.loads(recognizer.Result())["text"]
                if ui:
                    ui.partial = ""
                dispatched = listener.on_utterance(text, peak)
                peak = 0
                if dispatched:
                    # Drop audio buffered while the command was running.
                    while not audio.empty():
                        audio.get_nowait()
                    recognizer.Reset()
            elif ui:
                ui.partial = json.loads(recognizer.PartialResult())["partial"]


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
    use_ui = sys.stdout.isatty() and "--plain" not in argv and "--wav" not in argv
    ui = None
    if use_ui:
        from ui import UI
        ui = UI(LOG_PATH)
        ui.start()
        jarvis.hooks.log = ui.log
        jarvis.hooks.state = ui.set_state
        jarvis.hooks.action = ui.apply_action
        jarvis.hooks.result = lambda text: setattr(ui, "action", text)
        jarvis.hooks.engine = lambda text: setattr(ui, "engine", text)
    try:
        if ui:
            ui.boot(f"Loading {os.path.basename(MODEL_DIR)} ...", 0.2)
        recognizer = KaldiRecognizer(Model(MODEL_DIR), RATE)
        if ui:
            ui.boot("Speech engine ........ ONLINE")
            devices = jarvis.get_devices()
            ui.set_devices(devices)
            ui.boot(f"Govee link ........... {len(devices)} bulbs")
            ui.boot("Claude (subscription) . READY")
            ui.boot("")
            ui.boot("Say 'Jarvis' to begin.", 0.8)
        listener = Listener(dry="--dry" in argv, ui=ui)
        if "--wav" in argv:
            listen_wav(listener, recognizer, argv[argv.index("--wav") + 1])
            return
        listen_mic(listener, recognizer)
    except KeyboardInterrupt:
        pass
    except Exception:
        if ui:
            ui.log("CRASH: " + traceback.format_exc().splitlines()[-1])
        raise
    finally:
        if ui:
            ui.stop()
            print("bye")


if __name__ == "__main__":
    main(sys.argv)
