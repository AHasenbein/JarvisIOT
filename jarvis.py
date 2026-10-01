"""Jarvis: natural language -> Govee light actions.

    python3 jarvis.py                  # interactive prompt
    python3 jarvis.py "rainbow in the living room"

Two paths, cheapest first:
  1. Fast path: simple phrases ("lights on", "living room blue", "brightness fifty",
     "rainbow") are parsed locally. No LLM, no tokens, instant.
  2. Claude: everything else goes to a warm `claude` CLI session (your subscription,
     no API key). The session is restarted every RECYCLE_AFTER commands so context
     stays small.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import govee

CLI_MODEL = "haiku"
RECYCLE_AFTER = 3   # commands per Claude session before it is restarted
LLM_TIMEOUT = 60
DEVICE_TTL = 600    # seconds to cache the device list

SYSTEM_PROMPT = """You turn a spoken request into smart-light actions. Reply with ONLY a JSON array, no prose.

Each action: {{"target": <string|null>, "action": <string>, "value": <any>}}
- target: a room/device name from the list below (e.g. "living room"), or null for ALL lights.
  "lights on" => target null. "living room lights on" => target "living room".
- action/value:
  "on" (value null) | "off" (value null)
  "brightness" (value integer 1-100)
  "color" (value [r,g,b], each 0-255; pick sensible RGB for any color name)
  "temp" (value kelvin 2000-9000; warm ~2700, neutral ~4000, cool ~6500)
  "scene" (value: one exact name from the scene list below)
- Map vibes to scenes when it fits ("rainbow" => "Rainbow", "party" => "Party", "candles" => "Candlelight").
- Several requests in one sentence => several actions, in order.
- If the request is not about lights, reply [].
Each user message is one new request; earlier ones are unrelated.

Device names: {devices}
Scenes: {scenes}"""

VALID_ACTIONS = {"on", "off", "brightness", "color", "temp", "scene"}

# Output hooks. Plain terminal by default; voice.py swaps these for the animated UI.
hooks = SimpleNamespace(
    log=print,
    state=lambda state, hold=None: None,
    action=lambda kind, value, devices: None,
    result=lambda text: None,
    engine=lambda text: None,
)

# --- fast path -------------------------------------------------------------

FILLER = {
    "hey", "jarvis", "please", "turn", "switch", "set", "make", "the", "lights", "light",
    "lamp", "lamps", "bulb", "bulbs", "in", "my", "to", "it", "a", "all", "of", "at",
    "percent", "brightness", "color", "run",  # "run" = common mishearing of "turn"
}
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}


def words_to_int(tokens):
    """['fifty', 'five'] -> 55, ['40'] -> 40, anything else -> None."""
    if not tokens:
        return None
    total = 0
    for t in tokens:
        if t.isdigit():
            total += int(t)
        elif t in _UNITS:
            total += _UNITS[t]
        elif t in _TENS:
            total += _TENS[t]
        elif t == "hundred" and total:
            total *= 100
        else:
            return None
    return total


def fast_path(text, device_names, scene_names):
    """Return a list of actions if `text` is a simple known command, else None."""
    text = text.lower().replace("%", " percent ")
    for ch in ".,!?":
        text = text.replace(ch, " ")
    text = " ".join(text.split())

    target = None
    for name in sorted(device_names, key=len, reverse=True):
        if name.lower() in text:
            target = name
            text = text.replace(name.lower(), " ")
            break

    # Scene names can contain filler words ("Night Light"), so try them before stripping.
    bare = " ".join(t for t in text.split() if t not in ("hey", "jarvis", "please", "the", "a", "in", "my", "to", "set", "turn", "switch", "make", "it"))
    for scene in scene_names:
        if bare == scene.lower() and scene.lower() not in ("warm",):
            return [{"target": target, "action": "scene", "value": scene}]

    tokens = [t for t in text.split() if t not in FILLER]
    if not tokens:
        return None
    phrase = " ".join(tokens)

    def act(action, value=None):
        return [{"target": target, "action": action, "value": value}]

    if phrase in ("on", "off"):
        return act(phrase)
    if phrase in govee.COLORS:
        return act("color", list(govee.COLORS[phrase]))
    if phrase in ("warm", "cool"):
        return None  # ambiguous (temperature vs. the "Warm" scene): let Claude decide
    for scene in scene_names:
        if phrase == scene.lower():
            return act("scene", scene)
    level = words_to_int(tokens)
    if level is not None and 1 <= level <= 100:
        return act("brightness", level)
    return None


# --- Claude (warm CLI session) ---------------------------------------------

class Claude:
    """A long-lived `claude -p` process fed one JSON message per request."""

    def __init__(self, system_prompt):
        self.system_prompt = system_prompt
        self.proc = None
        self.lines = None
        self.count = 0

    def _start(self):
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("`claude` CLI not found on PATH")
        # Subscription login only: a stray API key in the environment must not take over.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        self.proc = subprocess.Popen(
            [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", CLI_MODEL, "--system-prompt", self.system_prompt,
             "--tools", "", "--disable-slash-commands", "--no-session-persistence"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=tempfile.gettempdir(), env=env,
        )
        self.lines = queue.Queue()
        self.count = 0
        threading.Thread(target=self._pump, args=(self.proc, self.lines), daemon=True).start()

    @staticmethod
    def _pump(proc, lines):
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # EOF: process died

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def ask(self, text):
        if self.proc is None or self.proc.poll() is not None or self.count >= RECYCLE_AFTER:
            self.close()
            self._start()
        msg = {"type": "user", "message": {"role": "user", "content": f"Request: {text}"}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + LLM_TIMEOUT
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                self.close()
                raise RuntimeError("Claude timed out")
            if line is None:
                self.close()
                raise RuntimeError("Claude session ended unexpectedly")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                self.count += 1
                if event.get("is_error"):
                    raise RuntimeError(f"Claude error: {str(event.get('result'))[:200]}")
                return event.get("result", "")


def extract_json(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in reply: {text[:200]}")
    return json.loads(text[start:end + 1])


# --- state shared across commands ------------------------------------------

_devices = {"at": 0.0, "list": []}
_claude = None


def get_devices():
    if time.time() - _devices["at"] > DEVICE_TTL or not _devices["list"]:
        _devices["list"] = govee.find_devices()
        _devices["at"] = time.time()
    return _devices["list"]


def get_claude(devices):
    global _claude
    if _claude is None:
        names = sorted({d["deviceName"] for d in devices})
        scenes = sorted(govee.list_scenes(devices[0]))
        _claude = Claude(SYSTEM_PROMPT.format(devices=", ".join(names), scenes=", ".join(scenes)))
    return _claude


def interpret(text, devices):
    names = sorted({d["deviceName"] for d in devices})
    actions = fast_path(text, names, list(govee.list_scenes(devices[0])))
    if actions is not None:
        return actions, "fast path"
    hooks.state("thinking")
    return extract_json(get_claude(devices).ask(text)), "claude"


def run_action(a):
    """Validate and execute one action dict against matching bulbs."""
    kind, target, value = a.get("action"), a.get("target"), a.get("value")
    if kind not in VALID_ACTIONS:
        raise ValueError(f"unknown action {kind!r}")
    fns = {
        "on": lambda d: govee.power(d, True),
        "off": lambda d: govee.power(d, False),
        "brightness": lambda d: govee.brightness(d, value),
        "color": lambda d: govee.color(d, tuple(max(0, min(255, int(c))) for c in value)),
        "temp": lambda d: govee.color_temp(d, max(2000, min(9000, int(value)))),
        "scene": lambda d: govee.scene(d, value),
    }
    devs = govee.find_devices(target)
    for d in devs:
        fns[kind](d)
    hooks.action(kind, value, devs)
    return f"{kind}{'' if value is None else ' ' + str(value)} -> {target or 'all lights'} ({len(devs)} bulb{'s' if len(devs) != 1 else ''})"


def handle(text):
    t0 = time.time()
    try:
        actions, engine = interpret(text, get_devices())
    except (Exception, SystemExit) as e:
        hooks.log(f"  couldn't interpret that: {e}")
        hooks.result("couldn't interpret that")
        hooks.state("error", 3)
        return
    hooks.engine(f"{engine} ({time.time() - t0:.1f}s)")
    if not actions:
        hooks.log("  (nothing to do with the lights)")
        hooks.result("nothing to do with the lights")
        hooks.state("idle")
        return
    ok = True
    for a in actions:
        try:
            msg = run_action(a)
            hooks.log("  " + msg)
            hooks.result(msg)
        except (Exception, SystemExit) as e:
            ok = False
            hooks.log(f"  failed {a}: {e}")
            hooks.result(f"failed: {e}")
    hooks.state("success" if ok else "error", 3)


def main(argv):
    govee.load_env()
    if len(argv) > 1:
        handle(" ".join(argv[1:]))
        return
    print("Jarvis ready. Type a request (Ctrl-D or 'quit' to exit).")
    try:
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text.lower() in ("quit", "exit"):
                break
            if text:
                handle(text)
    finally:
        if _claude:
            _claude.close()


if __name__ == "__main__":
    main(sys.argv)
