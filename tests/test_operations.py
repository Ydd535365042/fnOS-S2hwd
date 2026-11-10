"""Functional and authorization regressions for the 15 operations features.

python -I -B tests/test_operations.py
No real hardware operations or external notifications are executed.
"""
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_security import Sandbox, serving, request, load, SOURCE
sys.path.insert(0, str(SOURCE / "server"))
import s2ops
import s2store
import s2hardware
import job_runner


def configuration():
    return {"fan": {"tempProfile": "quiet", "hdd": dict(zip(["t1", "t2", "t3", "t4", "d2", "d3"], [42, 48, 52, 55, 30, 70])),
                    "cpu": dict(zip(["t1", "t2", "t3", "t4", "d2", "d3"], [60, 65, 72, 80, 30, 70]))},
        "led": {"power": "auto", "sata": "auto"}, "beep": {"ms": 280, "boot_beep": True},
        "schedule": {"enabled": False, "shutdown_time": "03:00", "boot_time": "08:00", "weekdays": list(range(7)), "skip_dates": [], "maintenance_until": 0},
        "guard": {"enabled": False, "cpu_limit": 80, "disk_limit": 55, "fail_samples": 3}}


class Device:
    def __init__(self):
        self.config = configuration()
        self.sensor = {"cpu_temp": 52, "hdd_max_temp": 38, "fan_duty": 45, "cpu_pct": 12,
                       "mem": {"used_pct": 35}, "services": {"fan-control": "failed"}}
        self.hardware = {"disks": [{"name": "sda", "serial": "fixture-drive-1", "model": "test",
                                    "smart": {"health": "ok", "reallocated": 0, "power_on_hours": 10}}]}
        self.alerts = {"events": [], "active": [], "last_id": 0}
        self.calls = []
        self.now = time.time()
        self.network_calls = []
        self.fail_delivery = False

    def call(self, op, params):
        self.calls.append((op, params))
        data = {}
        if op == "config_snapshot": data = copy.deepcopy(self.config)
        elif op == "config_validate": s2hardware.validate_bundle(params["config"])
        elif op == "config_apply": self.config = copy.deepcopy(s2hardware.validate_bundle(params["config"]))
        elif op == "schedule_info":
            data = {**self.config["schedule"], "rtc_wake": True, "timezone": "fixture",
                    "next_shutdown": s2hardware.schedule.next_event(self.config["schedule"], "shutdown", self.now),
                    "next_boot": s2hardware.schedule.next_event(self.config["schedule"], "boot", self.now)}
        elif op == "set_power_schedule": self.config["schedule"] = s2hardware.schedule.validate({**self.config["schedule"], **params})
        elif op == "set_maintenance": self.config["schedule"]["maintenance_until"] = params["until"]
        elif op == "fan_guard_info": data = {**self.config["guard"], "active": False, "reason": ""}
        elif op == "set_fan_guard": self.config["guard"] = s2hardware.validate_guard(params)
        elif op == "hwinfo": data = copy.deepcopy(self.hardware)
        elif op == "diagnostics_files": data = [{"id": "fan-conf", "title": "配置文件", "ok": True, "repair": "", "detail": "可读"}]
        elif op == "service": self.sensor["services"][params["name"]] = "active"
        else: raise AssertionError("Unexpected operation " + op)
        return {"ok": True, "data": data}

    def network(self, url, headers=None, body=None):
        self.network_calls.append((url, headers, body))
        if self.fail_delivery: raise OSError("simulated transport failure")
        if url.endswith("/api/sensor"): return {"ok": True, "data": copy.deepcopy(self.sensor)}
        if url.endswith("/api/alerts"): return {"ok": True, "data": copy.deepcopy(self.alerts)}
        return {"ok": True}


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="s2-ops-test-")
        self.device = Device()
        self.ops = s2ops.Operations(self.temp.name, self.device.call, lambda: self.device.sensor,
                                   lambda: self.device.alerts, clock=lambda: self.device.now, network=self.device.network)
        self.admin = {"name": "fixture-admin", "role": "admin", "scopes": s2store.ALL_SCOPES}
        self.ops.post("retention", {"days": 30, "enabled": True}, self.admin)

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_restore_preview_conflict_and_persistence(self):
        backup = self.ops.backup("before", "admin")
        self.assertEqual(backup["config"], configuration())
        modified = copy.deepcopy(backup["config"]); modified["beep"]["ms"] = 400
        preview = self.ops.restore_preview({"config": modified})
        self.assertIn("400", preview["diff"])
        self.device.config["beep"]["ms"] = 500
        with self.assertRaises(s2store.Problem): self.ops.restore(preview, "admin")
        preview = self.ops.restore_preview({"config": modified})
        self.ops.restore(preview, "admin")
        self.assertEqual(self.device.config["beep"]["ms"], 400)
        self.assertGreaterEqual(len(self.ops.get("versions", {}, self.admin)), 3)
        reopened = s2store.Store(self.temp.name)
        with reopened.db() as db: self.assertGreaterEqual(db.execute("SELECT count(*) FROM versions").fetchone()[0], 3)
        self.assertNotIn("password", json.dumps(backup))

    def test_backup_rejects_untrusted_fields_and_invalid_curve(self):
        config = configuration(); config["files"] = {"/etc/shadow": "bad"}
        self.assertRaises(ValueError, self.ops.restore_preview, {"config": config})
        config = configuration(); config["fan"]["cpu"]["t2"] = 1
        self.assertRaises(ValueError, self.ops.restore_preview, {"config": config})

    def test_history_retention_and_bounded_query(self):
        self.ops.sample(); self.device.now += 31; self.device.sensor["cpu_temp"] = 61; self.ops.sample()
        history = self.ops.history({"hours": "1"})
        self.assertEqual([r["cpu_temp"] for r in history], [52, 61])
        self.ops.store.set("retention_days", 1); self.device.now += 86460; self.ops.sample()
        self.assertEqual(len(self.ops.history({"hours": "72"})), 1)
        self.assertRaises(s2store.Problem, self.ops.history, {"hours": "0"})

    def test_notification_dedup_retry_and_secret_redaction(self):
        self.ops.save_notifications({"enabled": True, "kind": "webhook", "url": "https://notify.example/alerts", "secret": "fixture-secret"})
        self.device.alerts = {"events": [{"id": 1, "key": "cpu", "title": "高温"}], "active": [], "last_id": 1}
        self.device.fail_delivery = True
        self.ops.enqueue_alerts(); self.ops.enqueue_alerts()
        for _ in range(4): self.ops.deliver_pending(); self.device.now += 4000
        public = self.ops.notifications()
        self.assertNotIn("fixture-secret", json.dumps(public))
        self.assertEqual(len(public["deliveries"]), 1)
        self.assertEqual(public["deliveries"][0]["attempts"], 4)
        self.assertEqual(public["deliveries"][0]["status"], "failed")
        self.assertEqual(len(self.device.network_calls), 4)

    def test_maintenance_suppresses_services_but_delivers_thermal_alert(self):
        self.ops.set_maintenance({"minutes": 30, "reason": "test"})
        self.ops.save_notifications({"enabled": True, "kind": "webhook", "url": "https://notify.example/alerts"})
        self.device.alerts = {"events": [{"id": 1, "key": "svc:fan-control", "title": "服务停止"}, {"id": 2, "key": "cpu", "title": "CPU 高温"}], "last_id": 2}
        self.ops.enqueue_alerts(); self.ops.deliver_pending()
        states = {row["status"] for row in self.ops.notifications()["deliveries"]}
        self.assertEqual(states, {"suppressed", "sent"})
        self.assertEqual(len(self.device.network_calls), 1)
        self.assertTrue(self.ops.maintenance()["active"])
        self.device.now += 1801
        self.assertFalse(self.ops.maintenance()["active"])

    def test_changing_notification_destination_does_not_forward_old_secret(self):
        self.ops.save_notifications({"enabled": True, "kind": "webhook", "url": "https://a.example/notify", "secret": "private-credential"})
        self.ops.save_notifications({"enabled": True, "kind": "webhook", "url": "https://b.example/notify", "secret": ""})
        self.assertFalse(self.ops.notifications()["config"]["has_secret"])

    def test_smtp_requires_tls_before_authentication(self):
        smtp = mock.MagicMock(); smtp.__enter__.return_value = smtp; smtp.sock.getpeername.return_value = ("192.168.2.5", 587)
        config = {"kind": "email", "host": "mail.example", "port": 587, "username": "fixture", "password": "secret", "sender": "a@example.test", "recipient": "b@example.test"}
        with mock.patch.object(s2ops.smtplib, "SMTP", return_value=smtp): self.ops.send_notification(config, {"title": "fixture"})
        names = [c[0] for c in smtp.mock_calls]
        self.assertLess(names.index("starttls"), names.index("login"))
        smtp.send_message.assert_called_once()

    def test_health_diagnostics_and_constrained_repair(self):
        report = self.ops.report()
        self.assertTrue(any(not item["ok"] and item["repair"] for item in report["diagnostics"]))
        self.assertRaises(s2store.Problem, self.ops.post, "diagnostics/repair", {"service": "unapproved-service"}, self.admin)
        self.ops.post("diagnostics/repair", {"service": "fan-control"}, self.admin)
        self.assertEqual(self.device.sensor["services"]["fan-control"], "active")
        self.assertIn("generated", report)

    def test_weekly_schedule_skip_maintenance_and_execution_guard(self):
        monday = datetime.datetime(2026, 11, 9, 8, 0).timestamp()
        config = configuration()["schedule"]
        config.update(enabled=True, shutdown_time="09:00", weekdays=[0], skip_dates=["2026-11-09"])
        event = s2hardware.schedule.next_event(config, "shutdown", monday)
        self.assertEqual(event["local"], "2026-11-16 09:00:00")
        self.assertFalse(s2hardware.schedule.due(config, monday + 3600))
        config["skip_dates"] = []; self.assertTrue(s2hardware.schedule.due(config, monday + 3600))
        config["maintenance_until"] = int(monday + 7200)
        self.assertFalse(s2hardware.schedule.due(config, monday + 3600))
        self.assertRaises(ValueError, s2hardware.schedule.validate, {**config, "weekdays": ["0"]})
        self.assertRaises(ValueError, s2hardware.schedule.validate, {**config, "skip_dates": ["2026-02-30"]})

    def test_guard_config_validation(self):
        self.ops.post("guard", {"enabled": True, "cpu_limit": 75, "disk_limit": 52, "fail_samples": 2}, self.admin)
        self.assertEqual(self.ops.get("guard", {}, self.admin)["cpu_limit"], 75)
        self.assertRaises(ValueError, s2hardware.validate_guard, {"enabled": True, "cpu_limit": 999})

    def test_audit_actor_filter_and_persistence(self):
        self.ops.store.audit("set_fan", "alice", "192.0.2.4", True)
        self.ops.store.audit("set_fan", "bob", "192.0.2.5", False)
        rows = self.ops.get("audit", {"actor": "alice", "action": "fan"}, self.admin)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["ip"], "192.0.2.4")

    def test_account_management_is_removed(self):
        for action in (lambda: self.ops.get("users", {}, self.admin), lambda: self.ops.post("users", {}, self.admin)):
            with self.assertRaises(s2store.Problem) as error: action()
            self.assertEqual(error.exception.status, 404)
        with self.ops.store.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone())

    def test_api_keys_expire_and_revoke_without_storing_token(self):
        store = self.ops.store
        issued = store.create_key({"label": "monitor", "scopes": ["read"], "days": 1})
        self.assertEqual(store.key_principal(issued["token"])["scopes"], ["read"])
        self.assertNotIn(issued["token"], json.dumps(store.keys()))
        with mock.patch.object(s2store.time, "time", return_value=time.time() + 90000): self.assertIsNone(store.key_principal(issued["token"]))
        self.ops.post("keys/revoke", {"id": issued["id"]}, self.admin)
        self.assertIsNone(store.key_principal(issued["token"]))

    def test_fleet_aggregates_with_server_side_credentials(self):
        created = self.ops.save_device({"label": "NAS", "url": "https://nas.example", "token": "s2k_fixture"})
        fleet = self.ops.fleet()
        self.assertTrue(fleet[0]["online"])
        self.assertNotIn("s2k_fixture", json.dumps(fleet))
        self.assertEqual(self.device.network_calls[0][1]["Authorization"], "Bearer s2k_fixture")
        self.device.fail_delivery = True
        self.device.now += 16
        self.assertFalse(self.ops.fleet()[0]["online"])
        self.ops.post("devices/delete", created, self.admin); self.assertEqual(self.ops.devices(), [])

    def test_network_rejects_special_targets_credentials_and_redirect(self):
        for address in ("127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"):
            self.assertRaises(s2store.Problem, s2ops.check_address, address)
        for url in ("file:///etc/passwd", "https://user:pass@example.test", "http://example.test\r\nHost:bad"):
            self.assertRaises(s2store.Problem, s2ops.endpoint, url)
        conn = mock.MagicMock(); conn.sock.getpeername.return_value = ("192.168.2.30", 80)
        conn.getresponse.return_value.status = 302; conn.getresponse.return_value.read.return_value = b""
        with mock.patch.object(s2ops.http.client, "HTTPConnection", return_value=conn):
            self.assertRaises(s2store.Problem, s2ops.network_json, "http://nas.example/", {"Authorization": "Bearer fixture"})
        self.assertEqual(conn.request.call_count, 1)

    def test_disk_smart_trends_and_standby_skip(self):
        self.ops.sample(); self.device.now += 601
        self.device.hardware["disks"][0]["smart"]["reallocated"] = 3
        self.ops.sample()
        data = self.ops.disk_history({})
        self.assertEqual(next(c for c in data["changes"] if c["metric"] == "reallocated")["delta"], 3)
        self.device.now += 601; self.device.hardware["disks"][0]["standby"] = True; self.ops.sample()
        self.assertEqual(len(self.ops.disk_history({})["points"]), 2)

    def test_supervisor_normal_completion_timeout_and_log_limit(self):
        previous = signal.getsignal(signal.SIGTERM)
        try:
            for source, timeout, expected in (("print('fixture completed')", 5, 0), ("import time; time.sleep(10)", 1, 124), ("import time; print('x' * 2000, flush=True); time.sleep(10)", 5, 125)):
                script = Path(self.temp.name) / ".s2job-test.py"; script.write_text(source, encoding="utf-8")
                log, result = str(Path(self.temp.name) / "job.log"), str(Path(self.temp.name) / "job.rc")
                code = job_runner.supervise(sys.executable, str(script), log, result, timeout, self.temp.name, log_limit=1024)
                self.assertEqual(code, expected); self.assertEqual(int(Path(result).read_text()), expected)
                self.assertFalse(script.exists())
        finally:
            signal.signal(signal.SIGTERM, previous)


class HardwareIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="s2-hardware-test-")
        self.root = Path(self.temp.name)
        import hwctl
        self.hw = hwctl
        attributes = {"ETC_FAN": "fan.conf", "LED_OVERRIDE_CONF": "led.conf", "APP_BEEP_CONF": "beep.conf", "APP_SCHED_CONF": "schedule.conf", "APP_CONF_DIR": "conf", "FAN_STATUS": "fan-status", "FAN_RUN_DIR": "fan-run", "SCHED_APPLY_BIN": "apply.sh"}
        self.patches = [mock.patch.object(hwctl, key, str(self.root / value)) for key, value in attributes.items()]
        self.patches += [mock.patch.object(hwctl, "run", return_value=(0, "fixture")), mock.patch.object(hwctl, "restart_service", return_value=(True, "fixture")), mock.patch.object(hwctl, "_fan_script_has", return_value=True), mock.patch.object(hwctl, "rtc_wake_supported", return_value=True), mock.patch.dict(os.environ, {"S2_SCHEDULE_HELPER": str(SOURCE / "firmware/s2_schedule.py")})]
        for patch in self.patches: patch.start()
        (self.root / "fan.conf").write_text("TEMP_PROFILE=quiet\n", encoding="utf-8")
        (self.root / "apply.sh").touch()
        self.hw._KV_CACHE.clear()

    def tearDown(self):
        for patch in reversed(self.patches): patch.stop()
        self.temp.cleanup()

    def test_real_config_apply_and_rollback(self):
        ok, baseline = self.hw.dispatch("config_snapshot", {})
        self.assertTrue(ok)
        modified = copy.deepcopy(baseline); modified["beep"]["ms"] = 700
        ok, message = self.hw.dispatch("config_apply", {"config": modified})
        self.assertTrue(ok, message)
        self.assertEqual(self.hw.dispatch("config_snapshot", {})[1]["beep"]["ms"], 700)
        ok, message = self.hw.dispatch("config_apply", {"config": baseline, "expected": "0" * 64})
        self.assertFalse(ok); self.assertIn("重新预览", message)
        self.assertEqual(self.hw.dispatch("config_snapshot", {})[1]["beep"]["ms"], 700)
        before = (self.root / "fan.conf").read_text()
        modified["fan"]["tempProfile"] = "performance"
        with mock.patch.object(self.hw, "restart_service", return_value=(False, "fixture-failure")):
            ok, message = self.hw.dispatch("config_apply", {"config": modified})
        self.assertFalse(ok); self.assertIn("回滚", message)
        self.assertEqual((self.root / "fan.conf").read_text(), before)

    def test_schedule_roundtrip_and_missing_runtime_rejection(self):
        params = {"enabled": True, "shutdown_time": "21:00", "boot_time": "08:00", "weekdays": [0, 2, 4], "skip_dates": ["2026-12-25"]}
        self.assertTrue(self.hw.dispatch("set_power_schedule", params)[0])
        state = self.hw.dispatch("schedule_info", {})[1]
        self.assertEqual(state["weekdays"], [0, 2, 4]); self.assertIsNotNone(state["next_boot"])
        until = int(time.time()) + 600
        self.assertTrue(self.hw.dispatch("set_maintenance", {"until": until})[0])
        self.assertEqual(self.hw.dispatch("schedule_info", {})[1]["maintenance_until"], until)
        with mock.patch.dict(os.environ, {"S2_SCHEDULE_HELPER": str(self.root / "missing.py")}):
            self.assertFalse(self.hw.dispatch("set_power_schedule", params)[0])

    def test_guard_roundtrip_and_firmware_capability_check(self):
        config = {"enabled": True, "cpu_limit": 77, "disk_limit": 53, "fail_samples": 2}
        self.assertTrue(self.hw.dispatch("set_fan_guard", config)[0])
        self.assertEqual(self.hw.dispatch("fan_guard_info", {})[1]["cpu_limit"], 77)
        with mock.patch.object(self.hw, "_fan_script_has", return_value=False):
            self.assertFalse(self.hw.dispatch("set_fan_guard", config)[0])

    def test_actual_firmware_guard_function(self):
        shell = Path(os.environ.get("S2_TEST_SH", r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\usr\bin\sh.exe"))
        if not shell.exists():
            import shutil
            found = shutil.which("bash")
            if not found: self.skipTest("bash unavailable")
            shell = Path(found)
        script = (SOURCE / "firmware/fan-control.sh").read_text(encoding="utf-8")
        section = script[script.index('GUARD_CONF='):script.index('# ---------- 主循环 ----------')]
        conf = self.root / "guard.conf"; conf.write_text("ENABLED=1\nCPU_LIMIT=80\nDISK_LIMIT=55\nFAIL_SAMPLES=2\n", newline="\n")
        program = "log() { :; }\n" + section + '\ncheck_guard 0 35; echo "$guard_active"\ncheck_guard 0 35; echo "$guard_active"\ncheck_guard 60 35; check_guard 60 35; echo "$guard_active"\ncheck_guard 85 35; check_guard 85 35; echo "$guard_active"\ncheck_guard 79 35; check_guard 79 35; echo "$guard_active"\n'
        result = subprocess.run([str(shell)], input=program.encode(), capture_output=True, env={**os.environ, "S2_GUARD_CONF": str(conf).replace("\\", "/")}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.decode().split(), ["0", "1", "0", "1", "1"])

    def test_schedule_apply_reports_timer_and_rtc_failures(self):
        shell = Path(os.environ.get("S2_TEST_SH", r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\usr\bin\sh.exe"))
        if not shell.exists():
            import shutil
            found = shutil.which("bash")
            if not found: self.skipTest("bash unavailable")
            shell = Path(found)
        script = (SOURCE / "firmware/s2-power-schedule.sh").read_text(encoding="utf-8")
        section = script[script.index('apply() {'):script.index('# 只重设 RTC')]
        # Only the real apply function is executed; all system and RTC effects are stubs.
        program = section + '''
read_conf() { ENABLED=1; SHUTDOWN_TIME=21:00; BOOT_TIME=08:00; }
systemctl() { return "$SYSTEM_RC"; }
find_rtc() { echo fixture; }
next_epoch() { echo 100000; }
arm_rtc() { return "$RTC_RC"; }
clear_rtc() { :; }
SYSTEM_RC=1; RTC_RC=0; apply >/dev/null; echo "$?"
SYSTEM_RC=0; RTC_RC=0; apply >/dev/null; echo "$?"
SYSTEM_RC=0; RTC_RC=1; apply >/dev/null; echo "$?"
'''
        result = subprocess.run([str(shell)], input=program.encode(), capture_output=True,
            env={**os.environ, "PATH": str(shell.parent) + os.pathsep + os.environ.get("PATH", ""),
                 "SHUTDOWN_TIMER": str(self.root / "timer.conf").replace("\\", "/")}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.decode().split(), ["1", "0", "1"], result.stderr.decode(errors="replace"))


class RouteAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Sandbox(); cls.web = cls.box.web
        cls.device = Device()
        cls.web._OPS = s2ops.Operations(str(cls.box.root / "operations"), cls.device.call, lambda: cls.device.sensor, lambda: cls.device.alerts)
        cls.context = serving(cls.box.handler); cls.server = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None); cls.box.close()

    def headers(self, scopes=None):
        if scopes is not None:
            token = self.web.get_ops().store.create_key({"label": "test-client", "scopes": scopes})["token"]
        else:
            status, _, data = request(self.server, "/login", "token=fixture-password", {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 200)
            token = data["session"]
        return {"Authorization": "Bearer " + token, "X-S2-Request": "1", "Content-Type": "application/json"}

    def test_read_key_cannot_reach_write_admin_or_scripts_routes(self):
        headers = self.headers(["read"])
        self.assertEqual(request(self.server, "/api/ops/report", headers=headers, method="GET")[0], 200)
        for path, body in (("/api/hw", {"op": "set_fan"}), ("/api/ops/keys", {"label": "escalation"}), ("/api/script/start", {"name": "x.sh"}), ("/api/v1/ops/maintenance", {"minutes": 10})):
            with self.subTest(path=path): self.assertEqual(request(self.server, path, body, headers)[0], 403)
        self.assertEqual(request(self.server, "/api/ops/users", headers=headers, method="GET")[0], 403)

    def test_control_key_cannot_restore_or_manage_settings(self):
        headers = self.headers(["read", "control"])
        self.assertEqual(request(self.server, "/api/ops/maintenance", {"minutes": 10}, headers)[0], 200)
        for path in ("/api/ops/restore", "/api/ops/users", "/api/ops/devices", "/api/script/save"):
            self.assertEqual(request(self.server, path, {}, headers)[0], 403)

    def test_keep_alive_requests_do_not_reuse_previous_identity(self):
        import http.client
        admin, reader = self.headers(), self.headers(["read"])
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/ops/keys", headers=admin)
            response = connection.getresponse(); self.assertEqual(response.status, 200); response.read()
            connection.request("GET", "/api/ops/keys", headers=reader)
            response = connection.getresponse(); self.assertEqual(response.status, 403); response.read()
        finally:
            connection.close()

    def test_control_only_key_can_control_but_not_read_or_manage(self):
        issued = self.web.get_ops().store.create_key({"label": "automation", "scopes": ["control"]})
        headers = {"Authorization": "Bearer " + issued["token"], "Content-Type": "application/json", "X-S2-Request": "1"}
        self.assertEqual(request(self.server, "/api/ops/maintenance", {"minutes": 0}, headers)[0], 200)
        self.assertEqual(request(self.server, "/api/ops/users", headers=headers, method="GET")[0], 403)
        self.assertEqual(request(self.server, "/api/state", headers=headers, method="GET")[0], 403)

    def test_legacy_account_login_and_sessions_are_rejected(self):
        status = request(self.server, "/login", "username=old-user&token=fixture-password", {"Content-Type": "application/x-www-form-urlencoded"})[0]
        self.assertEqual(status, 400)
        token = self.web.SESSIONS.issue({"kind": "user", "name": "old-user", "role": "admin"})
        self.assertEqual(request(self.server, "/api/state", headers={"Authorization": "Bearer " + token}, method="GET")[0], 401)
        owner = self.headers()
        self.assertEqual(request(self.server, "/api/ops/users", headers=owner, method="GET")[0], 404)
        self.assertEqual(request(self.server, "/api/ops/users", {}, owner)[0], 404)

    def test_api_key_read_scope_and_admin_endpoint_rejection(self):
        issued = self.web.get_ops().store.create_key({"label": "monitor", "scopes": ["read"]})
        headers = {"Authorization": "Bearer " + issued["token"], "Content-Type": "application/json", "X-S2-Request": "1"}
        self.assertEqual(request(self.server, "/api/state", headers=headers, method="GET")[0], 200)
        self.assertEqual(request(self.server, "/api/ops/keys", {}, headers)[0], 403)

    def test_script_changed_digest_and_duplicate_rejected(self):
        headers = self.headers()
        request(self.server, "/api/script/save", {"name": "review.sh", "content": "echo old\n"}, headers)
        inspected = request(self.server, "/api/script/inspect?name=review.sh", headers=headers, method="GET")[2]["data"]
        request(self.server, "/api/script/save", {"name": "review.sh", "content": "echo changed\n"}, headers)
        self.assertEqual(request(self.server, "/api/script/start", {"name": "review.sh", "sha256": inspected["sha256"]}, headers)[0], 409)
        latest = request(self.server, "/api/script/inspect?name=review.sh", headers=headers, method="GET")[2]["data"]
        with mock.patch.object(self.web, "managed_jobs", return_value=[{"id": "a" * 32, "name": "review.sh", "done": False}]):
            self.assertEqual(request(self.server, "/api/script/start", {"name": "review.sh", "sha256": latest["sha256"]}, headers)[0], 409)
        self.assertEqual(request(self.server, "/api/script/start", {"name": "review.sh", "sha256": latest["sha256"]}, headers)[0], 200)


if __name__ == "__main__": unittest.main(verbosity=2)
