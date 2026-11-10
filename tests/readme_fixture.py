"""Loopback-only README screenshot fixture. All data is simulated; no hardware calls."""
import copy
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_security import Sandbox, serving
from test_operations import Device
import s2ops

NOW = int(time.time())


class ReadmeSandbox(Sandbox):
    def __init__(self):
        self.device = Device()
        self.device.now = NOW
        self.tick = 0
        super().__init__()
        self.services = {name: "active" for name in self.hw.HW_SERVICES}
        self.device.config["schedule"].update(enabled=True, shutdown_time="23:30", boot_time="07:00",
                                             weekdays=[0, 1, 2, 3, 4], skip_dates=["2026-11-15"])
        self.device.config["guard"]["enabled"] = True
        self.device.sensor = {"root": True, "hostname": "fnOS-S2-DEMO", "cpu_temp": 49, "hdd_max_temp": 37,
            "fan_duty": 34, "fan_mode": "auto", "fan_source": "smartctl", "cpu_pct": 16,
            "services": self.services, "net_link": {"up": True, "iface": "eth0"},
            "mem": {"total_kb": 8*1024*1024, "used_kb": 3*1024*1024, "avail_kb": 5*1024*1024,
                    "used_pct": 37.5, "cached_kb": 1024*1024, "swap_total_kb": 0},
            "cpu": {"pct": 16, "per_core": [12, 22, 9, 21], "mhz": [1800]*4, "temp": 49, "load": [.42, .36, .28]},
            "net": {"rx_bps": 2.4*1024**2, "tx_bps": .8*1024**2}, "fs": []}
        self.device.hardware = {"system": {"os": "fnOS（演示环境）", "kernel": "Linux aarch64", "arch": "aarch64"},
            "cpu": {"model": "Rockchip RK3568", "cores": 4, "core_name": "Cortex-A55", "max_mhz": 1992},
            "memory": {"total": 8*1024**3}, "raids": [],
            "nics": [{"name": "eth0", "up": True, "ipv4": ["192.0.2.10/24"], "speed_mbps": 2500, "duplex": "full", "driver": "r8125"}],
            "disks": []}
        self.device.sensor["nics"] = [{"name": "eth0", "up": True, "rx_bps": 2.4*1024**2, "tx_bps": .8*1024**2, "speed_mbps": 2500}]
        for index, name in enumerate(["sda", "sdb"]):
            disk = {"name": name, "path": "/dev/"+name, "model": "SATA HDD · 演示磁盘", "serial": "DEMO-DISK-0"+str(index+1),
                    "bus": "sata", "rotational": True, "bytes": 4*1024**4, "slot": index+1,
                    "slot_name": "盘位 "+str(index+1), "standby": False, "role": "存储空间 "+str(index+1),
                    "usage": {"total": 4*1024**4, "used": (1.6+index*.3)*1024**4, "free": (2.4-index*.3)*1024**4},
                    "smart": {"health": "ok", "temp": 37-index, "power_on_hours": 480, "power_cycles": 12,
                              "reallocated": 0, "pending": 0, "uncorrectable": 0, "ts": NOW}}
            self.device.hardware["disks"].append(disk)
        self.device.sensor["disk_list"] = [dict(d, temp=37-i, busy_pct=2+i, rd_bps=512*1024, wr_bps=128*1024) for i,d in enumerate(self.device.hardware["disks"])]
        self.hw.collect_sensor.side_effect = self.sensor
        self.web._OPS = s2ops.Operations(str(self.root/"operations"), self.device.call, self.sensor,
            lambda: self.device.alerts, clock=lambda: self.device.now, network=self.device.network)
        self.web._OPS.post("retention", {"days": 30, "enabled": True}, {"name": "owner"})
        for index in range(121):
            self.device.now = NOW-3600+index*30
            self.web._OPS.sample()
        self.device.now = NOW
        self.web._OPS.backup("启用周计划前", "owner")
        self.web._OPS.backup("日常配置备份", "owner")
        for action in ["设置风扇保护", "保存周计划", "创建配置备份"]:
            self.web._OPS.store.audit(action, "owner", "192.0.2.10", True)
        scripts = Path(self.web.SCRIPTS_DIR); scripts.mkdir(parents=True, exist_ok=True)
        for source in (Path(__file__).resolve().parents[1]/"app/server/default_scripts").glob("*.sh"):
            (scripts/source.name).write_bytes(source.read_bytes())

    def sensor(self):
        self.tick += 1
        result = copy.deepcopy(self.device.sensor)
        result["cpu_temp"] = 49 + round(3*math.sin(self.tick/9), 1)
        result["hdd_max_temp"] = 37 + round(math.sin(self.tick/18), 1)
        result["fan_duty"] = 0  # Quiet curve: both demo temperatures are below their start thresholds.
        result["cpu"]["temp"] = result["cpu_temp"]
        return result

    def dispatch(self, operation, params):
        if operation == "status":
            return True, {**self.sensor(), "port": self.port, "app_ver": self.hw.APP_VER,
                "uptime": 259200, "now": NOW, "tz_offset": 28800, "board": "RK3568 NAS（演示）", "patches": [],
                "scripts": self.hw.list_scripts(), "conf": {"power_schedule": self.device.config["schedule"],
                    "beep": {"beep_ms": 280, "boot_beep": True}, "fan": {"TEMP_PROFILE": "quiet"},
                    "led_override": {"power": "auto", "sata": "auto"}}}
        if operation == "hwinfo": return True, self.device.hardware
        if operation in ("config_snapshot", "config_validate", "schedule_info", "fan_guard_info", "diagnostics_files"):
            result = self.device.call(operation, params)
            return True, result["data"]
        return False, "演示环境不执行硬件操作"


box = ReadmeSandbox()
class Handler(box.handler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # Screenshot teardown may close a request already being served.
try:
    with serving(Handler) as server:
        box.port = server.server_port
        print(json.dumps({"port": box.port}), flush=True)
        sys.stdin.readline()
finally:
    box.close()
