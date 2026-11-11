"""Native hwmon/LED controller. No DTB, GPIO, MAC or hostname mutations.

The sysfs root and disk reader are injectable so hardware safety can be tested offline.
"""
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import threading
import time

import s2security

_here = Path(__file__).resolve().parent
_disk_path = _here / "s2_disk.py"
if not _disk_path.exists(): _disk_path = _here.parent / "firmware/s2_disk.py"
_spec = importlib.util.spec_from_file_location("s2_native_disk", _disk_path)
disk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disk)
SERVICE = "fnos-s2-native"


def read(path, default=""):
    try: return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError): return default


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError): return None


def kv(path):
    return {k.strip(): v.split("#", 1)[0].strip().strip('"\'') for line in read(path).splitlines()
            if "=" in line and not line.lstrip().startswith("#") for k, v in [line.split("=", 1)]}


def curve(temp, values, prefix):
    if temp is None: return 0
    defaults = [60, 65, 72, 80, 30, 30, 70, 100] if prefix == "CPU" else [42, 48, 52, 55, 30, 30, 70, 100]
    t1, t2, t3, t4, d1, d2, d3, d4 = [int(values.get(prefix+"_"+key, default))
        for key, default in zip(["T1", "T2", "T3", "T4", "D1", "D2", "D3", "D4"], defaults)]
    if not 0 < t1 < t2 < t3 < t4 <= (85 if prefix == "CPU" else 60) or not 20 <= d1 <= d2 <= d3 <= d4 <= 100:
        raise ValueError("温控曲线无效")
    if temp < t1: return 0
    if temp >= t4: return d4
    for low, high, a, b in [(t1, t2, d1, d2), (t2, t3, d2, d3), (t3, t4, d3, d4)]:
        if temp < high: return round(a + (b-a)*(temp-low)/(high-low))


class Native:
    def __init__(self, root="/", clock=time.time, temperature_reader=None, persist_leds=True):
        self.root = Path(root)
        self.conf = self.root / "etc/s2hwctl"
        self.runtime = self.root / "run/fnos-s2-native"
        self.clock = clock
        self.temperature_reader = temperature_reader or disk.temperatures
        self.disk_at = None
        self.disks = {}
        self.curve_on = False   # the temperature curve (not manual / test / start-up) is what keeps the fan spinning
        self.original_leds = {}
        self.persist_leds = persist_leds
        self.led_history = self.root/"var/lib/fnos-s2/led-original.json"
        self.led_history_error = ""
        self.saved_leds = {}
        if persist_leds:
            try:
                data = json.loads(self.led_history.read_text(encoding="utf-8")) if self.led_history.exists() else {}
                if not isinstance(data, dict): raise ValueError()
                for label, values in data.items():
                    if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", label) or not isinstance(values, list) or len(values) != 2 or not all(isinstance(x,str) for x in values):
                        raise ValueError()
                    self.original_leds[str(self.root/"sys/class/leds"/label)] = tuple(values)
                self.saved_leds = data
            except (OSError, ValueError, TypeError): self.led_history_error = "灯光原状态记录无法读取，请先检查备份"

    def fan(self):
        matches = [p for p in sorted((self.root/"sys/class/hwmon").glob("hwmon*"))
                   if read(p/"name") in ("pwmfan", "pwm_fan", "pwm-fan") and (p/"pwm1").is_file()]
        return matches[0] if len(matches) == 1 else None

    def capabilities(self):
        fan = self.fan()
        model = read(self.root/"proc/device-tree/model") or read(self.root/"sys/firmware/devicetree/base/model")
        missing = "设备树已为 S2，但内核未提供唯一的标准风扇接口，请检查 pwm-fan 驱动" if "witxiaohui" in model.lower() else "未检测到唯一的 pwm-fan；请先准备匹配此设备的 DTB，本应用不会刷写引导"
        return {"mode": "native", "ready": fan is not None, "fan": str(fan) if fan else "",
                "leds": [p.name for p in self.leds()], "kernel_trip": self.cpu()[1],
                "detail": "标准 pwm-fan 已就绪" if fan else missing}

    def wait_ready(self, timeout=30, sleep=time.sleep, clock=time.monotonic):
        deadline = clock()+timeout
        while self.fan() is None and clock() < deadline: sleep(.25)
        return self.fan() is not None

    def cpu(self):
        temperatures, trips = [], []
        for zone in (self.root/"sys/class/thermal").glob("thermal_zone*"):
            kind = read(zone/"type").lower()
            if not any(s in kind for s in ("cpu", "soc")): continue
            value = number(read(zone/"temp"))
            if value is not None and 0 < value <= 125000: temperatures.append(value/1000)
            for path in zone.glob("trip_point_*_temp"):
                if read(path.with_name(path.name.replace("_temp", "_type"))) not in ("passive", "critical"): continue
                value = number(read(path))
                if value is not None and 0 < value <= 125000: trips.append(value/1000)
        return (max(temperatures) if temperatures else None, min(trips) if trips else None)

    def write(self, path, value):
        path = Path(path)
        if read(path) != str(value): path.write_text(str(value), encoding="ascii")

    def fan_write(self, duty):
        node = self.fan()
        if node is None: raise OSError("标准 pwm-fan 不可用")
        if (node/"pwm1_enable").exists(): self.write(node/"pwm1_enable", 1)
        pwm = round(max(0, min(100, duty))*255/100)
        self.write(node/"pwm1", pwm)
        actual = number(read(node/"pwm1"))
        if actual is None or (duty == 100 and actual < 255):
            raise OSError("风扇写入后回读未达到安全转速")
        return round(actual*100/255)

    def fan_tick(self):
        now = self.clock()
        if self.disk_at is None or now-self.disk_at >= 30:
            try: self.disks = dict(self.temperature_reader())
            except (OSError, ValueError): self.disks = {"sensor": "unknown"}
            self.disk_at = now
        cpu, trip = self.cpu()
        temps = [number(v) for v in self.disks.values() if number(v) is not None]
        highest = max(temps, default=0)
        unknown = any(v != "standby" and (number(v) is None or not 0 < number(v) <= 120) for v in self.disks.values())
        config = kv(self.conf/"fan.conf")
        reason, hold = "", ""
        try:
            duty = max(curve(cpu, config, "CPU"), curve(highest, config, "HDD"))
            # 2 ℃ hysteresis, only after the curve itself started the fan. The actual PWM is no guide:
            # it is also non-zero after start-up (full speed self-check), a full-speed test or manual control,
            # and treating those as "cooling down" kept the fan at its start duty below the start temperature.
            if duty == 0 and self.curve_on:
                near = [(int(config.get(prefix+"_D1", 30)), prefix, temp) for prefix, temp in (("CPU", cpu), ("HDD", highest if temps else None))
                        if temp is not None and curve(temp+2, config, prefix) > 0]
                if near:
                    duty, prefix, _ = max(near)
                    hold = "%s:%d" % (prefix.lower(), int(config.get(prefix+"_T1", 60 if prefix == "CPU" else 42)) - 2)
            self.curve_on = duty > 0
            hot = (cpu is not None and cpu >= int(config.get("CPU_T3", 72))) or highest >= int(config.get("HDD_T3", 52))
        except (ValueError, TypeError): duty, reason, hot = 100, "温控曲线无效", True
        if cpu is None: reason = "CPU 温度传感器无效"
        elif cpu >= min(85, trip-1 if trip is not None else 85): reason = "达到内核温控安全阈值"
        # The full-speed duty is user-adjustable, so a hot disk needs its own hard stop.
        elif highest >= 60: reason = "硬盘温度过高"
        force = number(read(self.runtime/"force_until")) or 0
        manual = number(read(self.runtime/"manual_duty"))
        manual = int(manual) if manual is not None and 30 <= manual <= 100 else None
        auto_duty = max(duty, 30 if unknown else 0)     # what auto mode gives right now, shown when leaving manual
        if reason: duty, mode = 100, "protect"
        elif force > now: duty, mode = 100, "test"
        # Manual speed may run below the curve only while both sources are under their third anchor;
        # from there on the curve wins whenever it asks for more.
        elif manual is not None: duty, mode = (max(manual, duty) if hot else manual), "manual"
        else: duty, mode = auto_duty, "auto"
        actual = self.fan_write(duty)
        status = {"timestamp": str(int(now)), "mode": mode, "guard_reason": reason,
                  "cpu_temp": "" if cpu is None else str(cpu), "hdd_max_temp": str(highest) if temps else "",
                  "hdd_source": "unknown" if unknown else "smartctl", "duty_pct": str(actual), "level": str(actual),
                  "manual_duty": "" if manual is None else str(manual),
                  "auto_duty": str(auto_duty), "hold": hold,
                  "disks": " ".join(f"{k}={v}" for k,v in self.disks.items())}
        return status

    def leds(self):
        return [p for p in sorted((self.root/"sys/class/leds").glob("*")) if (p/"brightness").exists()]

    def led(self, node, trigger="none", on=False):
        if self.led_history_error: raise ValueError(self.led_history_error)
        raw = read(node/"trigger")
        choices = raw.replace("[", "").replace("]", "").split()
        active = next((s[1:-1] for s in raw.split() if s.startswith("[")), "none")
        if str(node) not in self.original_leds:
            value = (active, read(node/"brightness", "0"))
            if self.persist_leds:
                updated = {**self.saved_leds, node.name: list(value)}
                s2security.atomic_text(str(self.led_history), json.dumps(updated), mode=0o600)
                self.saved_leds = updated
            self.original_leds[str(node)] = value
        if trigger not in choices and not (trigger == "none" and not (node/"trigger").exists()): return False
        if trigger != active: self.write(node/"trigger", trigger)
        if trigger in ("none", "timer"):
            self.write(node/"brightness", read(node/"max_brightness", "1") if on else 0)
        if trigger == "timer":
            self.write(node/"delay_on", 1000); self.write(node/"delay_off", 1000)
        return True

    def led_tick(self):
        config = kv(self.conf/"led-override.conf")
        interface = ""
        for line in read(self.root/"proc/net/route").splitlines()[1:]:
            values = line.split()
            if len(values) > 3 and values[1] == "00000000": interface = values[0]; break
        link = bool(interface and read(self.root/"sys/class/net"/interface/"carrier") == "1")
        yielding = (self.root/"run/led-boot").exists() or (self.root/"run/led-shutdown").exists()
        for node in self.leds():
            label = node.name.lower()
            if "power" in label.split(":") and not yielding:
                on = config.get("POWER_LED", "auto") != "off" and (("green" in label and link) or ("red" in label and not link))
                self.led(node, on=on)
            match = re.search(r"(?:^|:)(?:sata|disk|hdd)(\d+)(?:$|:)", label)
            if not match: continue
            mode, slot = config.get("SATA_LED", "auto"), match[1]
            if mode == "off": self.led(node); continue
            if "red" in label: self.led(node); continue
            if "green" not in label: continue
            sleeping = False
            if mode == "breathe":
                candidates = [p.name for p in (self.root/"sys/class/block").glob("sd*")
                    if re.search(r"/ata"+slot+r"/", str((p/"device").resolve()).replace("\\", "/"))]
                sleeping = len(candidates) == 1 and self.disks.get(candidates[0]) == "standby"
            if sleeping and self.led(node, "timer", True): continue
            choices = read(node/"trigger").replace("[", "").replace("]", "").split()
            trigger = next((s for s in ("ata"+slot, "disk-activity") if s in choices), None)
            if trigger: self.led(node, trigger)
            elif str(node) in self.original_leds: self.led(node, self.original_leds[str(node)][0])

    def beep(self, milliseconds=280, sleep=time.sleep):
        node = next((p for p in self.leds() if any(s in p.name.lower() for s in ("buzzer", "beeper"))), None)
        if node is None: return False, "未找到标准蜂鸣器 LED 节点"
        try:
            if not self.led(node, on=True): return False, "蜂鸣器不支持手动触发"
            sleep(max(30, min(5000, int(milliseconds)))/1000)
        finally:
            self.led(node)
        return True, "蜂鸣测试完成"

    def restore_leds(self):
        for name, (trigger, brightness) in self.original_leds.copy().items():
            if "power" in Path(name).name.split(":") and ((self.root/"run/led-boot").exists() or (self.root/"run/led-shutdown").exists()):
                continue
            try:
                self.led(Path(name), trigger)
                if trigger == "none": self.write(Path(name)/"brightness", brightness)
            except OSError: pass

    def control_stamp(self):
        stamp = []
        for path in (self.runtime/"force_until", self.runtime/"manual_duty", self.conf/"fan.conf", self.conf/"led-override.conf"):
            try: stamp.append(path.stat().st_mtime_ns)
            except OSError: stamp.append(0)
        return stamp

    def run(self):
        if not self.capabilities()["ready"]: raise RuntimeError(self.capabilities()["detail"])
        stopped = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM): signal.signal(signum, lambda *_: stopped.set())
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.fan_write(100)
        marker = self.runtime/"boot-beep-done"
        if not marker.exists():
            marker.touch()
            try:
                config = json.loads(read(self.conf/"beep.conf", "{}"))
                if config.get("boot_beep", True) and not (self.conf/"led-beautify.enabled").exists(): self.beep(config.get("beep_ms", 280))
            except (OSError, ValueError, TypeError): pass
        last_notice = None
        try:
            while not stopped.is_set():
                # Taken before the tick: a command written while the tick runs must still count as new.
                seen = self.control_stamp()
                try:
                    status = self.fan_tick()
                except (OSError, ValueError) as error:
                    try: actual = str(self.fan_write(100))
                    except OSError: actual = ""
                    status = {"mode": "protect", "duty_pct": actual, "guard_reason": "原生温控失败："+str(error),
                              "cpu_temp": "", "hdd_max_temp": "", "hdd_source": "unknown", "disks": ""}
                try:
                    self.led_tick()
                    status["led_error"] = ""
                except (OSError, ValueError) as error:
                    status["led_error"] = str(error)
                notice = status["guard_reason"] + " | " + status["led_error"]
                try:
                    s2security.atomic_text(str(self.runtime/"status"), "".join(f"{k}={str(v).replace(chr(10), ' ').replace(chr(13), ' ')}\n" for k,v in status.items()), mode=0o644)
                except (OSError, ValueError) as error:
                    notice += " | 状态保存失败："+str(error)
                if notice != last_notice:
                    print(notice.strip(" | ") or "原生温控正常", flush=True)
                    last_notice = notice
                # Sample every 2 s, but react within 0.25 s when the console changes a control file.
                for _ in range(8):
                    if stopped.wait(0.25) or self.control_stamp() != seen: break
        finally:
            self.restore_leds()
            self.fan_write(100)  # Stay safe until the original controller resumes.


if __name__ == "__main__":
    import sys
    native = Native()
    if sys.argv[1:] == ["run"]: native.run()
    elif sys.argv[1:] == ["--ready"]: sys.exit(0 if native.capabilities()["ready"] else 1)
    elif sys.argv[1:] == ["--wait-ready"]: sys.exit(0 if native.wait_ready() else 1)
    else: print(json.dumps(native.capabilities(), ensure_ascii=False))
