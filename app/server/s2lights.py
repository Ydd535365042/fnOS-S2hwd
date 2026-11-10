"""Optional boot/shutdown indicators using standard LED nodes only."""
import json
from pathlib import Path
import signal
import socket
import threading
import time

from s2native import Native, read
import s2security


class Indicators:
    def __init__(self, root="/", sleeper=time.sleep, clock=time.monotonic, ready=None):
        self.native = Native(root, persist_leds=False)
        self.run = Path(root)/"run"
        self.sleep, self.clock = sleeper, clock
        self.ready = ready or self.web_ready
        self.stopped = threading.Event()

    @staticmethod
    def web_ready():
        try:
            with socket.create_connection(("127.0.0.1", 80), timeout=.3): return True
        except OSError: return False

    def power(self, color):
        return next((p for p in self.native.leds() if "power" in p.name.split(":") and color in p.name.split(":")), None)

    def prompt(self):
        try: config = json.loads(read(self.native.conf/"beep.conf", "{}"))
        except ValueError: config = {}
        if not isinstance(config, dict): config = {}
        enabled = config.get("BEEP_PROMPT", config.get("boot_beep", True))
        if enabled in (False, 0, "0", "false", "off"): return
        try: milliseconds = max(30, min(600, int(config.get("beep_ms", 280))))
        except (ValueError, TypeError): milliseconds = 280
        try: self.native.beep(milliseconds, sleep=self.sleep)
        except OSError: pass  # An optional buzzer must not prevent boot/shutdown indication.

    def boot(self, timeout=300):
        self.run.mkdir(parents=True, exist_ok=True)
        if (self.run/"led-shutdown").exists(): return
        flag = self.run/"led-boot"
        s2security.atomic_text(str(flag), "fnos-s2\n", mode=0o644)
        green, red = self.power("green"), self.power("red")
        try:
            discovery_deadline = self.clock()+30
            while not green and not red and not self.stopped.is_set() and self.clock() < discovery_deadline:
                self.sleep(.5); green, red = self.power("green"), self.power("red")
            if not green and not red: raise OSError("标准电源灯节点未出现")
            if red: self.native.led(red)
            using_timer = bool(green and self.native.led(green, "timer", True))
            self.prompt()
            deadline = self.clock()+max(0, min(timeout, 300))
            on = False
            while not self.stopped.is_set() and not (self.run/"led-shutdown").exists() and self.clock() < deadline and not self.ready():
                if green and not using_timer:
                    on = not on; self.native.led(green, on=on)
                self.sleep(.5)
        finally:
            if green and not (self.run/"led-shutdown").exists(): self.native.led(green, on=True)
            flag.unlink(missing_ok=True)

    def shutdown(self, duration=30):
        self.run.mkdir(parents=True, exist_ok=True)
        flag = self.run/"led-shutdown"
        try:
            with flag.open("x") as stream: stream.write("fnos-s2\n")
            first = True
        except FileExistsError: first = False
        if not first: return
        green, red = self.power("green"), self.power("red")
        if red: self.native.led(red, "default-on", True) or self.native.led(red, on=True)
        if first: self.prompt()
        if not green: return
        self.native.led(green, on=True)
        deadline = self.clock()+max(0, min(duration, 30))
        brightness = green/"brightness"
        maximum = read(green/"max_brightness", "1")
        # Only the short shutdown phase uses software mixing, not a permanent 100Hz daemon.
        with brightness.open("w", encoding="ascii") as stream:
            while not self.stopped.is_set() and self.clock() < deadline:
                for value, delay in ((maximum, .0045), ("0", .0055)):
                    stream.seek(0); stream.write(value); stream.flush(); self.sleep(delay)
        self.native.write(brightness, maximum)


if __name__ == "__main__":
    import sys
    indicators = Indicators()
    for signum in (signal.SIGINT, signal.SIGTERM): signal.signal(signum, lambda *_: indicators.stopped.set())
    if sys.argv[1:] == ["boot"]: indicators.boot()
    elif sys.argv[1:] == ["shutdown"]: indicators.shutdown()
    else: raise SystemExit("usage: s2lights.py boot|shutdown")
