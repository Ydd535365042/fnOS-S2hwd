"""Loopback-only fixture for browser_security.mjs. Never executes hardware commands."""
import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_security import Sandbox, serving


class BrowserSandbox(Sandbox):
    def __init__(self):
        self.schedule = {"enabled": True, "shutdown_time": "09:00", "boot_time": "10:00"}
        self.fail_schedule = False
        self.posts = []
        self.started = time.monotonic()
        super().__init__()

    def dispatch(self, op, params):
        self.calls.append((op, params))
        if op == "status":
            now = datetime.datetime(2026, 11, 10, tzinfo=datetime.timezone.utc).timestamp()
            payload = '<img src=x onerror="window.xss=1">'
            return True, {"root": True, "hostname": "fixture-host", "port": self.port,
                "app_ver": "test", "uptime": 7200, "now": now + time.monotonic() - self.started,
                "tz_offset": 28800, "services": {}, "patches": [],
                "scripts": self.hw.list_scripts() if "--ops" in sys.argv else [{"name": payload + ".sh", "size": 32, "mtime": now}],
                "conf": {"power_schedule": self.schedule.copy()}}
        if op == "hwinfo":
            return True, {"system": {"os": "fixture", "kernel": "fixture", "arch": "arm64"}}
        if op == "set_power_schedule":
            if self.fail_schedule:
                return False, "模拟保存失败"
            self.schedule.update(params)
        return True, "模拟操作成功"


box = BrowserSandbox()
if "--ops" in sys.argv:
    from test_operations import Device
    import s2ops
    device = Device()
    device.sensor["services"]["fan-control"] = "active"
    box.web._OPS = s2ops.Operations(str(box.root / "operations"), device.call, lambda: device.sensor,
                                   lambda: device.alerts, clock=lambda: device.now, network=device.network)
    for i in range(12):
        device.now += 31
        device.sensor["cpu_temp"] = 48 + i
        box.web._OPS.sample()
    scripts = Path(box.web.SCRIPTS_DIR)
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "fixture.sh").write_text("#!/bin/sh\necho fixture-safe-test\n", encoding="utf-8")
    def job_stub(path, timeout=600, expected_hash=None):
        identifier = "a" * 32
        jobs = Path(box.web.JOBS_DIR)
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / (identifier + ".meta")).write_text(json.dumps({"name": Path(path).name, "timeout": timeout, "sha256": expected_hash}), encoding="utf-8")
        (jobs / (identifier + ".log")).write_text("fixture output\n", encoding="utf-8")
        (jobs / (identifier + ".rc")).write_text("0\n", encoding="ascii")
        box.jobs.append(path)
        return identifier
    box.web.start_job.side_effect = job_stub


class Handler(box.handler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # Headless browser teardown closes keep-alive sockets on Windows.

    def do_GET(self):
        if self.path == "/__fixture/stats":
            self._json({"ok": True, "calls": box.calls, "jobs": box.jobs, "posts": box.posts})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/__fixture/control":
            body, error = self._body()
            if error:
                self._json({"ok": False}, error)
                return
            if "password" in body:
                box.web.TOKEN = "fixture-password" if body["password"] else ""
            if "fail_schedule" in body:
                box.fail_schedule = body["fail_schedule"]
            if "schedule" in body:
                box.schedule.update(body["schedule"])
            self._json({"ok": True})
            return
        box.posts.append({"path": self.path, "marker": self.headers.get("X-S2-Request"),
                          "bearer": self.headers.get("Authorization", "").startswith("Bearer "),
                          "cookie": bool(self.headers.get("Cookie")), "origin": self.headers.get("Origin")})
        super().do_POST()


try:
    with serving(Handler) as server:
        box.port = server.server_port
        print(json.dumps({"port": server.server_port}), flush=True)
        sys.stdin.readline()
finally:
    box.close()
