"""Animated terminal UI for Jarvis: arc-reactor core, live mic waveform, ASCII bulbs.

Pure ANSI (truecolor) so it needs no extra packages. `python3 ui.py --demo` previews it.
"""
import colorsys
import math
import os
import shutil
import sys
import threading
import time
from collections import deque

FPS = 15
RAMP = " ·:░▒▓█"
BARS = " ▁▂▃▄▅▆▇█"
W_REACTOR, H_REACTOR = 31, 13
WIDTH = 92
RESET = "\x1b[0m"
DIM = (95, 110, 130)
WARM_WHITE = (255, 200, 120)

# state -> (base color, label, animation speed)
STATES = {
    "boot": ((90, 200, 255), "INITIALIZING", 3.0),
    "idle": ((60, 190, 255), "LISTENING FOR 'JARVIS'", 1.0),
    "wake": ((150, 255, 255), "YES? I'M LISTENING", 2.0),
    "heard": ((255, 255, 255), "COMMAND RECEIVED", 2.5),
    "thinking": ((255, 170, 40), "THINKING", 3.0),
    "success": ((80, 255, 140), "DONE", 1.5),
    "error": ((255, 70, 70), "UNABLE TO COMPLY", 0.6),
}


def fg(c, k=1.0):
    r, g, b = (max(0, min(255, int(v * k))) for v in c)
    return f"\x1b[38;2;{r};{g};{b}m"


def kelvin_to_rgb(k):
    k = max(2000, min(9000, k))
    if k <= 4000:
        f = (k - 2000) / 2000
        return (255, int(140 + 90 * f), int(50 + 140 * f))
    f = (k - 4000) / 5000
    return (int(255 - 70 * f), int(230 - 20 * f), 255)


def ring(d, r, w):
    return max(0.0, 1 - abs(d - r) / w)


def sectors(a, n, offset, duty):
    pos = ((a + offset) % (2 * math.pi)) / (2 * math.pi / n)
    return 1.0 if (pos % 1) < duty else 0.2


class UI:
    def __init__(self, log_path=None):
        self.log_path = log_path
        self.state = "boot"
        self.revert_at = 0.0
        self.t0 = time.time()
        self.state_since = self.t0
        self.hist = deque([0.0] * 400, maxlen=400)
        self.partial = ""
        self.heard = "-"
        self.action = "-"
        self.engine = "-"
        self.count = 0
        self.events = deque(maxlen=14)
        self.boot_lines = []
        self.lights = {}
        self.running = False

    # --- updates from the voice / jarvis threads ---------------------------
    def set_state(self, state, hold=None):
        self.state = state
        self.state_since = time.time()
        self.revert_at = time.time() + hold if hold else 0.0

    def push_level(self, v):
        self.hist.append(max(0.0, min(1.0, v)))

    def log(self, msg):
        msg = msg.strip()
        if not msg:
            return
        line = time.strftime("%H:%M:%S ") + msg
        self.events.append(line)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def boot(self, text, delay=0.3):
        self.boot_lines.append(text)
        time.sleep(delay)

    def set_devices(self, devices):
        for d in devices:
            self.lights[d["device"]] = {"name": d["deviceName"], "on": None, "rgb": WARM_WHITE,
                                        "bright": 100, "scene": None, "changed": 0.0}

    def apply_action(self, kind, value, devices):
        for d in devices:
            L = self.lights.setdefault(d["device"], {"name": d["deviceName"], "on": None, "rgb": WARM_WHITE,
                                                     "bright": 100, "scene": None, "changed": 0.0})
            L["changed"] = time.time()
            if kind == "on":
                L["on"] = True
            elif kind == "off":
                L["on"] = False
            elif kind == "color":
                L.update(on=True, rgb=tuple(value), scene=None)
            elif kind == "temp":
                L.update(on=True, rgb=kelvin_to_rgb(int(value)), scene=None)
            elif kind == "brightness":
                L.update(on=True, bright=int(value))
            elif kind == "scene":
                L.update(on=True, scene=str(value))

    # --- rendering ---------------------------------------------------------
    def reactor(self, t, state, W, H):
        base, _, speed = STATES[state]
        ts = t * speed
        age = time.time() - self.state_since
        Rmax = (H - 1) / 2
        cx, cy = W // 2, H // 2
        r1, r2, r3 = Rmax * 0.88, Rmax * 0.65, Rmax * 0.46
        w1, w2, w3 = max(0.6, Rmax * 0.11), max(0.5, Rmax * 0.09), max(0.4, Rmax * 0.07)
        n1, n2 = max(12, int(Rmax * 4)), max(6, int(Rmax))
        core = Rmax * 0.3
        sweep_r, sweep_w = Rmax * 0.76, max(0.8, Rmax * 0.13)
        out = []
        for y in range(H):
            row, last = [], None
            for x in range(W):
                px, py = (x - cx) * 0.5, y - cy
                d, a = math.hypot(px, py), math.atan2(py, px)
                v = ring(d, r1, w1) * sectors(a, n1, ts * 0.6, 0.6)
                v = max(v, ring(d, r2, w2) * sectors(a, n2, -ts, 0.5))
                v = max(v, ring(d, r3, w3) * 0.45)
                glow = 1 - d / (core * (1 + 0.2 * math.sin(ts * 2)))
                if glow > 0:
                    v = max(v, glow ** 1.3 * (0.75 + 0.25 * math.sin(ts * 3)))
                if state in ("wake", "heard"):
                    r = ((ts * 2) % 6) * Rmax / 6.5
                    v = max(v, ring(d, r, max(0.8, Rmax * 0.13)) * (1 - r / Rmax) * 1.3)
                if state == "thinking":
                    delta = (ts * 2 - a) % (2 * math.pi)
                    if delta < 1.8:
                        v = max(v, ring(d, sweep_r, sweep_w) * (1 - delta / 1.8) ** 2 * 1.5)
                if state == "success" and age < 0.8:
                    v = min(1.0, v + (0.8 - age) * ring(d, Rmax * (0.3 + age * 1.2), max(1.2, Rmax * 0.2)))
                v = min(1.0, v)
                ch = RAMP[int(v * (len(RAMP) - 1) + 0.5)]
                if ch != " ":
                    col = fg(base, 0.2 + 0.8 * v)
                    if col != last:
                        row.append(col)
                        last = col
                row.append(ch)
            out.append("".join(row) + RESET)
        return out

    def bulb_color(self, L, t, x):
        if L["on"] is None:
            return DIM, False
        if not L["on"]:
            return (60, 66, 80), False
        k = 0.25 + 0.75 * L["bright"] / 100
        s = (L["scene"] or "").lower()
        if s and any(w in s for w in ("candle", "fire", "sunset")):
            c = (255, int(120 + 40 * math.sin(t * 9 + x)), 30)
        elif s:
            c = tuple(int(255 * v) for v in colorsys.hsv_to_rgb((t * 0.25 + x * 0.18) % 1, 0.85, 1))
        else:
            c = L["rgb"]
        return tuple(int(v * k) for v in c), True

    def bulbs(self, t, cols):
        lights = list(self.lights.values())[:8]
        rows = [""] * 7
        slot = max(13, cols // max(1, len(lights)))
        for i, L in enumerate(lights):
            c, lit = self.bulb_color(L, t, i)
            pulse = time.time() - L["changed"] < 1.0
            edge = fg(c, 1.0) if lit else fg(c)
            fill = (fg(c, 1.0) + "█") if lit else (fg(DIM) + ("?" if L["on"] is None else " "))
            rays = (fg(c, 1.0) + ("\\  |  /" if (int(t * 6) % 2 or pulse) else " \\ | / ")) if lit else "       "
            cells = [
                f" {rays}   ",
                f"   {edge}_____{RESET}   ",
                f"  {edge}/{fill * 5}{edge}\\{RESET}  ",
                f" {edge}|{fill * 7}{edge}|{RESET} ",
                f"  {edge}\\{fill * 5}{edge}/{RESET}  ",
                f"   {edge}|===|{RESET}   ",
                f"{fg(DIM)}{L['name'][:13]:^13}{RESET}",
            ]
            pad = " " * ((slot - 13) // 2)
            for r in range(7):
                rows[r] += pad + cells[r] + pad
        return rows

    def frame(self, now=None):
        now = now or time.time()
        if self.revert_at and now > self.revert_at:
            self.set_state("idle")
        t = now - self.t0
        state = self.state
        base, label, _ = STATES[state]
        size = shutil.get_terminal_size((WIDTH, 34))
        cols, rows = max(60, size.columns - 1), max(24, size.lines)

        # Reactor fills the vertical space left over; it must also leave room for the text panel.
        H = max(9, min(45, rows - 15))
        H = min(H, int((cols * 0.55 - 3) / 4 * 2 + 1))
        H -= 1 - H % 2  # odd so the core is centred
        Rmax = (H - 1) / 2
        W = int(4 * Rmax) + 3
        panel_w = max(20, cols - W - 8)

        title = "J . A . R . V . I . S ."
        head = "".join(fg((60 + i * 8, 190 - i * 3, 255)) + ch for i, ch in enumerate(title))
        sub = "Just A Rather Very Intelligent System"
        lines = [" " * ((cols - len(title)) // 2) + head + RESET,
                 " " * ((cols - len(sub)) // 2) + fg(DIM) + sub + RESET,
                 "".join(fg(base, 0.25 + 0.35 * (0.5 + 0.5 * math.sin(i / 6 - t * 2))) + "─" for i in range(cols)) + RESET]
        dot = "●" if int(t * 2) % 2 == 0 else "○"
        lines.append(f"  {fg(base)}{dot} {label}{RESET}")

        left = self.reactor(t, state, W, H)
        label_ = lambda s: f"{fg(DIM)}{s:<10}{RESET}"
        txt = lambda s, c: fg(c) + s[:panel_w - 10] + RESET
        up = int(now - self.t0)
        if state == "boot":
            right = [""] * 2 + [f"{fg(base)}{l}{RESET}" for l in self.boot_lines[-(H - 2):]]
        else:
            live = self.partial if state == "idle" and self.partial else ""
            right = [
                "",
                label_("HEARING") + txt(live + "▌" if live else "", (150, 165, 190)),
                label_("HEARD") + txt(self.heard, (230, 240, 255)),
                label_("ACTION") + txt(self.action, (120, 255, 170)),
                label_("ENGINE") + txt(self.engine, (255, 200, 90)),
                label_("COMMANDS") + txt(str(self.count), (230, 240, 255)),
                label_("UPTIME") + txt(f"{up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}", (230, 240, 255)),
                "",
                fg(DIM) + "RECENT" + RESET,
            ] + [fg(DIM, 0.9) + e[:panel_w] + RESET for e in list(self.events)[-max(0, H - 9):]]
        right = (right + [""] * H)[:H]
        lines += [f"  {l}   {r}" for l, r in zip(left, right)]

        n = cols - 8
        hist = list(self.hist)[-n:]
        hist = [0.0] * (n - len(hist)) + hist
        wave = "".join(BARS[int(v ** 0.6 * (len(BARS) - 1) + 0.5)] for v in hist)
        lines += ["", f"  {fg(DIM)}MIC {RESET}{fg(base)}{wave}{RESET}", ""]
        lines += self.bulbs(t, cols)
        lines.append(f"  {fg(DIM, 0.8)}Ctrl-C to quit{RESET}")

        key = (cols, rows)
        prefix = "\x1b[2J" if key != getattr(self, "_size", key) else ""
        self._size = key
        return prefix + "\n".join(l + "\x1b[K" for l in lines[:rows])

    # --- lifecycle -----------------------------------------------------------
    def start(self):
        if os.name == "nt":
            os.system("")  # enables ANSI escape handling in the Windows console
        for stream in (sys.stdout,):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            try:
                sys.stdout.write("\x1b[H" + self.frame() + "\x1b[J")
                sys.stdout.flush()
            except Exception:
                pass
            time.sleep(1 / FPS)

    def stop(self):
        self.running = False
        time.sleep(0.1)
        sys.stdout.write("\x1b[?25h\x1b[?1049l" + RESET)
        sys.stdout.flush()


if __name__ == "__main__":
    ui = UI()
    ui.set_devices([{"device": str(i), "deviceName": n} for i, n in
                    enumerate(["living room 2", "Living room", "Smart LED Bulb", "Smart LED Bulb"])])
    ui.start()
    try:
        for line in ("Speech engine ... OK", "Govee link ... 4 bulbs", "Claude ... OK"):
            ui.boot(line, 0.6)
        ui.set_state("idle")
        script = [("wake", None, 2), ("heard", "turn the living room blue", 1), ("thinking", None, 2),
                  ("success", ("color", (0, 0, 255)), 3), ("idle", ("scene", "Rainbow"), 4),
                  ("success", ("off", None), 3)]
        for st, act, secs in script:
            ui.set_state(st)
            if st == "heard":
                ui.heard = act
            elif act:
                ui.apply_action(act[0], act[1], [{"device": str(i), "deviceName": "x"} for i in range(4)])
                ui.action = f"{act[0]} {act[1] or ''}"
            for _ in range(secs * FPS):
                ui.push_level(abs(math.sin(time.time() * 7)) * (0.7 if st in ("wake", "heard") else 0.08))
                time.sleep(1 / FPS)
    finally:
        ui.stop()
