"""Govee cloud API wrapper (stdlib only). Usage:

    python3 govee.py devices
    python3 govee.py on|off [name]

[name] is a case-insensitive substring and applies to every match ("living room"
hits both living room bulbs); omit it to target all bulbs.

    python3 govee.py brightness <1-100> [name]
    python3 govee.py color <name|#rrggbb> [name]
    python3 govee.py temp <2000-9000> [name]
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = "https://openapi.api.govee.com/router/api/v1"
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

COLORS = {
    "red": (255, 0, 0), "orange": (255, 100, 0), "yellow": (255, 220, 0),
    "green": (0, 255, 0), "cyan": (0, 255, 255), "blue": (0, 0, 255),
    "purple": (150, 0, 255), "pink": (255, 60, 150), "white": (255, 255, 255),
}


def load_env(path=ENV_PATH):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _request(method, path, body=None):
    key = os.environ.get("GOVEE_API_KEY", "")
    if not key or key == "paste-your-key-here":
        raise SystemExit("GOVEE_API_KEY is not set in .env")
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Govee-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Govee API error {e.code}: {e.read().decode(errors='replace')}")


def list_devices():
    return _request("GET", "/user/devices")["data"]


def find_devices(name=None):
    """All devices whose name contains `name`; with no name, every bulb (groups excluded)."""
    devices = [d for d in list_devices() if d["sku"] != "SameModeGroup"]
    if name:
        devices = [d for d in devices if name.lower() in d.get("deviceName", "").lower()]
        if not devices:
            raise SystemExit(f"No device matching '{name}'")
    if not devices:
        raise SystemExit("No devices found on this Govee account")
    return devices


def _control(dev, cap_type, instance, value):
    return _request("POST", "/device/control", {
        "requestId": str(uuid.uuid4()),
        "payload": {
            "sku": dev["sku"],
            "device": dev["device"],
            "capability": {"type": cap_type, "instance": instance, "value": value},
        },
    })


def power(dev, on):
    return _control(dev, "devices.capabilities.on_off", "powerSwitch", 1 if on else 0)


def brightness(dev, level):
    return _control(dev, "devices.capabilities.range", "brightness", max(1, min(100, int(level))))


def color(dev, rgb):
    r, g, b = rgb
    return _control(dev, "devices.capabilities.color_setting", "colorRgb", (r << 16) | (g << 8) | b)


def color_temp(dev, kelvin):
    return _control(dev, "devices.capabilities.color_setting", "colorTemperatureK", int(kelvin))


_scene_cache = {}


def list_scenes(dev):
    """{scene name: {id, paramId}} for this device's model (cached per sku)."""
    if dev["sku"] not in _scene_cache:
        r = _request("POST", "/device/scenes", {
            "requestId": str(uuid.uuid4()),
            "payload": {"sku": dev["sku"], "device": dev["device"]},
        })
        _scene_cache[dev["sku"]] = {
            o["name"]: o["value"]
            for cap in r["payload"]["capabilities"]
            if cap["instance"] == "lightScene"
            for o in cap["parameters"]["options"]
        }
    return _scene_cache[dev["sku"]]


def scene(dev, name):
    scenes = list_scenes(dev)
    match = next((n for n in scenes if n.lower() == name.lower()), None) \
        or next((n for n in scenes if name.lower() in n.lower()), None)
    if not match:
        raise SystemExit(f"No scene matching '{name}'")
    return _control(dev, "devices.capabilities.dynamic_scene", "lightScene", scenes[match])


def parse_color(text):
    text = text.lower().lstrip("#")
    if text in COLORS:
        return COLORS[text]
    if len(text) == 6:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    raise SystemExit(f"Unknown color '{text}'")


def main(argv):
    load_env()
    if len(argv) < 2:
        print(__doc__)
        return
    cmd, args = argv[1], argv[2:]
    if cmd == "devices":
        for d in list_devices():
            print(f"{d.get('deviceName')}  sku={d['sku']}  id={d['device']}")
        return
    if cmd in ("on", "off"):
        action, name = (lambda d: power(d, cmd == "on")), (args[0] if args else None)
    elif cmd == "brightness":
        action, name = (lambda d: brightness(d, args[0])), (args[1] if len(args) > 1 else None)
    elif cmd == "color":
        rgb = parse_color(args[0])
        action, name = (lambda d: color(d, rgb)), (args[1] if len(args) > 1 else None)
    elif cmd == "temp":
        action, name = (lambda d: color_temp(d, args[0])), (args[1] if len(args) > 1 else None)
    elif cmd == "scene":
        action, name = (lambda d: scene(d, args[0])), (args[1] if len(args) > 1 else None)
    else:
        print(__doc__)
        return
    for dev in find_devices(name):
        print(dev["deviceName"], action(dev))


if __name__ == "__main__":
    main(sys.argv)
