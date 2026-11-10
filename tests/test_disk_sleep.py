"""Disk-sleep regressions. Hardware is simulated; only temporary files are written."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/server"))
import hwctl
import hwinfo
import s2ops


class SleepTests(unittest.TestCase):
    def test_smart_standby_unknown_and_active_responses(self):
        reader = hwinfo.disk_reader
        with mock.patch.object(reader, "device_type", return_value="ata"):
            for rc, payload, state in [
                (3, {"power_mode": "STANDBY"}, "standby"),
                (3, {"power_mode": "SLEEP"}, "standby"),
                (5, {"temperature": {"current": 42}}, "unknown"),
                (0, {"smartctl": {"messages": []}}, "unknown"),
                (0, {"temperature": {"current": 42}}, "active"),
                (8, {"temperature": {"current": 55}, "smart_status": {"passed": False}}, "active"),
            ]:
                with self.subTest(rc=rc, state=state):
                    runner = mock.Mock(return_value=(rc, json.dumps(payload)))
                    data, result = reader.read_smart("/dev/sda", runner=runner, find=lambda _: "smartctl")
                    self.assertEqual(result, state)
                    args = runner.call_args.args[0]
                    self.assertEqual(args[args.index("-d") + 1], "ata")
                    self.assertEqual(args[args.index("-n") + 1], "standby,3,5")
                    self.assertEqual(data is not None, state == "active")

    def test_unknown_bridge_never_uses_device_autodetection(self):
        reader = hwinfo.disk_reader
        runner = mock.Mock()
        with mock.patch.object(reader.os.path, "realpath", return_value="/sys/devices/usb1/1-1/host0/target0/0:0:0:0"):
            self.assertEqual(reader.read_smart("/dev/sda", runner=runner, find=lambda _: "smartctl"), (None, "unknown"))
        runner.assert_not_called()
        with mock.patch.object(reader.os.path, "realpath", return_value="/sys/devices/platform/fe210000.sata/ata1/host0/target0/0:0:0:0"):
            self.assertEqual(reader.device_type("/dev/sda"), "ata")

    def test_nvme_keeps_health_collection_without_ata_power_query(self):
        runner = mock.Mock(return_value=(0, '{"nvme_smart_health_information_log": {"temperature": 41}}'))
        _, state = hwinfo.disk_reader.read_smart("/dev/nvme0n1", runner=runner, find=lambda _: "smartctl")
        args = runner.call_args.args[0]
        self.assertEqual(state, "active")
        self.assertEqual(args[args.index("-d") + 1], "nvme")
        self.assertNotIn("-n", args)

    def test_hwinfo_fallback_does_not_report_unknown_as_healthy(self):
        with mock.patch.object(hwinfo.disk_reader, "device_type", return_value="ata"), \
             mock.patch.object(hwinfo, "which", return_value="smartctl"), \
             mock.patch.object(hwinfo, "run", return_value=(5, '{"temperature":{"current":42}}')):
            self.assertEqual(hwinfo.smartctl_json("/dev/sda"), (None, False))

    def test_install_migration_preserves_backup_and_other_modules(self):
        reader = hwinfo.disk_reader
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            conf = root / "etc/modules-load.d/drivetemp.conf"
            conf.parent.mkdir(parents=True)
            original = "# user config\ndrivetemp # old S2 default\nother_module\n"
            conf.write_text(original)
            reader.disable_drivetemp(root)
            first = conf.read_text()
            self.assertIn("other_module", first)
            self.assertNotIn("drivetemp #", first)
            reader.disable_drivetemp(root)
            self.assertEqual(first, conf.read_text())
            self.assertEqual(conf.with_suffix('.conf.s2-before-sleep.bak').read_text(), original)
            self.assertIn("install drivetemp /bin/false", (root / "etc/modprobe.d/s2-no-drivetemp.conf").read_text())

    def test_module_unload_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "sys/module/drivetemp").mkdir(parents=True)
            with mock.patch.object(hwinfo.disk_reader, "run", return_value=(1, "busy")):
                with self.assertRaises(OSError): hwinfo.disk_reader.disable_drivetemp(root)
            self.assertTrue((root / "etc/modprobe.d/s2-no-drivetemp.conf").exists())

    def test_module_unload_success_and_fresh_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); module = root / "sys/module/drivetemp"; module.mkdir(parents=True)
            def unload(argv, timeout):
                self.assertEqual(argv, ["modprobe", "-r", "drivetemp"])
                module.rmdir()
                return 0, ""
            with mock.patch.object(hwinfo.disk_reader, "run", side_effect=unload):
                hwinfo.disk_reader.disable_drivetemp(root)
            self.assertFalse(module.exists())
            self.assertNotIn("drivetemp", [line.strip() for line in (root / "etc/modules-load.d/drivetemp.conf").read_text().splitlines() if not line.startswith("#")])

    def test_native_unknown_power_does_not_issue_smart_commands(self):
        with mock.patch.object(hwinfo, "_sg_open", return_value=123), \
             mock.patch.object(hwinfo, "ata_check_power_mode", return_value=None), \
             mock.patch.object(hwinfo, "_ata_smart_read") as read, \
             mock.patch.object(hwinfo.os, "close"):
            data, standby, error = hwinfo.smart_read_native("/dev/sda", "sda", True)
            self.assertIsNone(data); self.assertFalse(standby); self.assertTrue(error)
            read.assert_not_called()

    def test_enabled_notifications_with_no_new_events_do_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            network = mock.Mock()
            ops = s2ops.Operations(folder, lambda *_: {"ok": True, "data": {}}, lambda: {},
                                   lambda: {"events": [], "last_id": 0}, network=network)
            ops.save_notifications({"enabled": True, "kind": "webhook", "url": "https://nas.example/notify"})
            before = Path(ops.store.path).read_bytes()
            for _ in range(3): ops.sample()
            self.assertEqual(before, Path(ops.store.path).read_bytes())
            network.assert_not_called()

    def test_history_can_resume_and_pause_without_deleting_records(self):
        with tempfile.TemporaryDirectory() as folder:
            args = (folder, lambda *_: {"ok": True, "data": {"disks": []}}, lambda: {}, lambda: {})
            ops = s2ops.Operations(*args)
            ops.post("retention", {"days": 30, "enabled": True}, {"name": "admin"})
            ops.sample()
            ops.post("retention", {"days": 30, "enabled": False}, {"name": "admin"})
            before = Path(ops.store.path).read_bytes()
            with mock.patch.object(ops.store, "db", side_effect=AssertionError("disabled collector touched disk")):
                ops.sample()
            self.assertEqual(before, Path(ops.store.path).read_bytes())
            restored = s2ops.Operations(*args)
            self.assertFalse(restored.history_enabled)
            self.assertEqual(len(restored.history({})), 1)

    def test_collectors_never_read_drivetemp_even_before_standby_check(self):
        for collector in ("sensor", "hwinfo"):
            with self.subTest(collector=collector):
                reads = []
                def listing(path):
                    path = str(path).replace("\\", "/")
                    if path == "/sys/class/hwmon": return ["hwmon2"]
                    if path == "/sys/block" or path.endswith("/device/block"): return ["sda"]
                    return []
                def reading(path, default=""):
                    path = str(path).replace("\\", "/")
                    if path.endswith("/hwmon2/name"): return "drivetemp"
                    if path.endswith("/temp1_input"):
                        reads.append(path)
                        return "41000"
                    if path.endswith("/rotational"): return "1"
                    if path.endswith("/size"): return "100000"
                    return default
                disk = dict(name="sda", path="/dev/sda", model="fixture", serial="fixture", firmware="",
                    vendor="", bytes=51200000, bus="sata", rotational=True, removable=False)
                with contextlib.ExitStack() as stack:
                    for target, values in [
                        (hwinfo.os, {"listdir": listing}),
                        (hwinfo.os.path, {"isdir": lambda _: True, "exists": lambda _: False}),
                        (hwinfo, {"rd": reading, "disk_is_standby": lambda *_: True,
                            "enumerate_disks": lambda: [disk], "read_filesystems": lambda: [],
                            "read_meminfo_ext": lambda: {"total_kb": 1}, "read_cpu_per_core": lambda: [],
                            "cpu_mhz_list": lambda: [], "read_cpu_load_triplet": lambda: [], "read_nic_rates": lambda: []}),
                        (hwctl, {"sysfs_read": reading, "read_diskstats": lambda: {}, "read_cpu_pct": lambda: 0,
                            "_thermal_zone_cpu_temp": lambda: 50, "read_loadavg": lambda: {}, "read_net_rate": lambda: {}}),
                    ]:
                        for key, value in values.items(): stack.enter_context(mock.patch.object(target, key, side_effect=value))
                    result = hwctl._sys_fields()["disk_list"] if collector == "sensor" else hwinfo.read_disks(True, True)
                self.assertTrue(result[0]["standby"])
                self.assertEqual(reads, [], "standby must be preserved without any drivetemp reads")

    def test_background_history_disabled_by_default_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            ops = s2ops.Operations(folder, lambda *_: {"ok": True, "data": {"disks": []}},
                lambda: {}, lambda: {"events": [], "last_id": 0})
            original = Path(ops.store.path).read_bytes()
            for _ in range(3): ops.sample()
            self.assertEqual(original, Path(ops.store.path).read_bytes())
            with ops.store.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM samples").fetchone()[0], 0)

    def test_fan_has_no_ordering_cycle_with_its_install_target(self):
        unit = (ROOT / "app/firmware/fan-control.service").read_text()
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("After=multi-user.target", unit)

    def test_configuration_wizard_preserves_operations_storage_location(self):
        shell = Path(os.environ.get("S2_TEST_SH", r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\usr\bin\sh.exe"))
        if not shell.exists(): self.skipTest("POSIX shell unavailable")
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.conf"
            settings.write_text("PORT=18899\nTOKEN=fixture\nS2_OPERATIONS_DIR='/ssd/operations data'\n", newline="\n")
            result = subprocess.run([str(shell)], input=(ROOT / "fpk/cmd/config_callback").read_bytes().replace(b"\r\n", b"\n"),
                capture_output=True, timeout=10, env={**os.environ, "PATH": str(shell.parent) + os.pathsep + os.environ.get("PATH", ""),
                    "TRIM_PKGETC": folder.replace("\\", "/"), "wizard_port": "18900", "wizard_token": "", "wizard_clear_token": "false"})
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertIn("S2_OPERATIONS_DIR='/ssd/operations data'", settings.read_text(encoding="utf-8"))
            self.assertIn("PORT=18900", settings.read_text(encoding="utf-8"))

    def test_fan_loop_discards_temperature_after_standby_or_failed_read(self):
        shell = Path(os.environ.get("S2_TEST_SH", r"C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\usr\bin\sh.exe"))
        if not shell.exists(): self.skipTest("POSIX shell unavailable")
        source = (ROOT / "app/firmware/fan-control.sh").read_text(encoding="utf-8")
        start = source.index('GUARD_CONF=')
        with tempfile.TemporaryDirectory() as folder:
            program = '''
CHECK_INTERVAL=1; HDD_SMART_CYCLES=1; FACTORY_TEST_CYCLE=10; TEMP_DEBOUNCE_CNT=0; USE_GPIO_FORCE=0
HDD_T1=42; HDD_T2=48; HDD_T3=52; HDD_T4=55; HDD_D1=30; HDD_D2=30; HDD_D3=70; HDD_D4=100
CPU_T1=60; CPU_T2=65; CPU_T3=72; CPU_T4=80; CPU_D1=30; CPU_D2=30; CPU_D3=70; CPU_D4=100
FORCE_FILE="$TEST_ROOT/force"; RELOAD_FILE="$TEST_ROOT/reload"; GUARD_CONF="$TEST_ROOT/guard"
log() { :; }; trim_log() { :; }; init_pwm() { return 0; }; conf_mtime() { echo 1; }
get_cpu_temp() { echo 40; }; set_fan_speed_pwm() { :; }; sleep() { :; }
read_disk_temp_smart() { case "$cycle_count" in 0) echo sda=60 ;; 1) echo sda=standby ;; *) echo sda=unknown ;; esac; }
write_status() { echo "$3|$4|$5|$1"; [ "$cycle_count" -lt 2 ] || exit 0; }
'''
            curve = source[source.index('curve_duty() {'):source.index('# ---------- 状态输出')]
            program += curve + source[start:]
            result = subprocess.run([str(shell)], input=program.encode(), capture_output=True, timeout=10,
                env={**os.environ, "PATH": str(shell.parent) + os.pathsep + os.environ.get("PATH", ""),
                     "TEST_ROOT": folder.replace("\\", "/"), "S2_GUARD_CONF": (folder + "/guard").replace("\\", "/")})
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(result.stdout.decode().splitlines(), ["60|smartctl|sda=60|100", "0|smartctl|sda=standby|0", "0|unknown|sda=unknown|30"])


if __name__ == "__main__":
    unittest.main()
