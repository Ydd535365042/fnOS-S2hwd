"""Install only the native controller; DTB changes require the explicit dtb action."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re
import shutil
import sys

import s2dtb
import s2native
import s2security

from s2activation import Activation, HARDWARE

BOOT = ["selfheal.service", "selfheal-hourly.timer", "selfheal-hourly.service", "boot-version-guard.service", "fix-fnenv-shutdown.service"]
OWN = "fnos-s2-native.service"
LIGHTS = ["fnos-s2-led-boot.service", "fnos-s2-led-shutdown.service"]


class Deployment(Activation):
    allowed_units = Activation.allowed_units | frozenset(BOOT + LIGHTS + ["led-boot.service", "led-shutdown.service",
        "fnos-s2-dtb-guard.service", "fnos-s2-dtb-guard.path", "fnos-s2-scsi-rescan.service"])
    def install(self, source):
        source = Path(source)
        files = {
            "server/s2native.py": "usr/local/lib/fnos-s2/s2native.py",
            "server/s2activation.py": "usr/local/lib/fnos-s2/s2activation.py",
            "server/s2deploy.py": "usr/local/lib/fnos-s2/s2deploy.py",
            "server/s2dtb.py": "usr/local/lib/fnos-s2/s2dtb.py",
            "server/s2lights.py": "usr/local/lib/fnos-s2/s2lights.py",
            "server/s2security.py": "usr/local/lib/fnos-s2/s2security.py",
            "firmware/s2_disk.py": "usr/local/lib/fnos-s2/s2_disk.py",
            "dtb/"+s2dtb.NAME: "usr/local/lib/fnos-s2/"+s2dtb.NAME,
            "agent/fnos-s2-native.service": "etc/systemd/system/"+OWN,
            "agent/fnos-s2-dtb-guard.service": "usr/local/lib/fnos-s2/units/fnos-s2-dtb-guard.service",
            "agent/fnos-s2-dtb-guard.path": "usr/local/lib/fnos-s2/units/fnos-s2-dtb-guard.path",
            "agent/fnos-s2-scsi-rescan.service": "usr/local/lib/fnos-s2/units/fnos-s2-scsi-rescan.service",
        }
        for name in ("s2_schedule.py", "s2-power-schedule.sh"):
            files["firmware/"+name] = "usr/local/sbin/"+name
        for name in ("s2-power-schedule.service", "s2-power-shutdown.service", "s2-power-shutdown.timer"):
            files["firmware/"+name] = "etc/systemd/system/"+name
        for name in LIGHTS: files["agent/"+name] = "etc/systemd/system/"+name
        for ident in ("s2-selfheal", "s2-lights"):
            base = "etc/s2hwctl/patches.d/" + ident + "/"
            installed = self.root / base / "module.conf"
            if installed.exists() and "BUNDLED=1" not in installed.read_text(encoding="utf-8"):
                raise ValueError("存在同名自定义模块，未覆盖：" + ident)
            for name in ("module.conf", "module.sh", "bundle.json", "package.run"):
                files["bundled/"+ident+"/"+name] = base + name
            files["server/s2bundle.py#"+ident] = base + "runner.py"
        # Validate all sources before replacing any installed file.
        content = {target: (source/name.split("#")[0]).read_bytes() for name,target in files.items()}
        s2dtb.validate(content["usr/local/lib/fnos-s2/"+s2dtb.NAME])
        tracked = list(content) + ["etc/s2-power-schedule.conf", "etc/s2hwctl/backend", "etc/s2hwctl/fan.conf"]
        previous = {name: (s2dtb.read_regular(self.root/name, optional=True), (self.root/name).stat().st_mode & 0o777 if (self.root/name).exists() else None) for name in tracked}
        services = self.snapshot(HARDWARE + [OWN, "s2-power-schedule.service"])
        self.state.mkdir(parents=True, exist_ok=True)
        (self.root/"etc/s2hwctl").mkdir(parents=True, exist_ok=True)
        try:
            for target, data in content.items():
                s2dtb.atomic(self.root/target, data)
                if target.endswith((".py", ".sh")): os.chmod(self.root/target, 0o755)
            schedule = self.root/"etc/s2-power-schedule.conf"
            if not schedule.exists(): s2dtb.atomic(schedule, b"ENABLED=0\nSHUTDOWN_TIME=\nBOOT_TIME=\n")
            self.system("daemon-reload")
            if self.native.capabilities()["ready"] or s2dtb.state(self.root)["prepared"]: self.system("enable", OWN)
            if s2native.kv(schedule).get("ENABLED") == "1": self.system("enable", "s2-power-schedule.service")
            if self.native.capabilities()["ready"]:
                self.system("restart", OWN)
                self.system("is-active", "--quiet", OWN)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.system("disable", "--now", OWN, required=False)
            for name, (data, mode) in previous.items():
                if data is None: (self.root/name).unlink(missing_ok=True)
                else: s2dtb.atomic(self.root/name, data, mode=mode)
            self.system("daemon-reload", required=False)
            self.restore(services)
            raise
        return self.native.capabilities()

    def lights(self, enabled=True):
        marker = self.root/"etc/s2hwctl/led-beautify.enabled"
        if enabled:
            if not self.native.capabilities()["ready"]:
                raise ValueError("请先完成设备树升级并重启，待原生硬件就绪后启用灯光美化")
            self.system("is-active", "--quiet", OWN)
            current = self.snapshot(["led-boot.service", "led-shutdown.service", "fix-fnenv-shutdown.service"])
            # Never replace an existing hook that may also repair boot files.
            if "fix-fnenv-shutdown.service" in current:
                raise ValueError("存在旧 fix-fnenv-shutdown 关机钩子，请先核对并迁移，未改动该钩子")
            own = self.snapshot(LIGHTS)
            prior_marker = s2dtb.read_regular(marker, optional=True)
            if prior_marker and all(own.get(unit, {}).get("UnitFileState") == "enabled" for unit in LIGHTS) and own.get(LIGHTS[0], {}).get("ActiveState") != "failed":
                return {"enabled": True, "changed": False}
            self.remember("light-services.json", current)
            try:
                self.stop(current)
                s2security.atomic_text(str(marker), "enabled\n", mode=0o644)
                self.system("enable", *LIGHTS)
                self.system("start", LIGHTS[0])
            except (OSError, ValueError, subprocess.SubprocessError):
                if prior_marker is None: marker.unlink(missing_ok=True)
                else: s2dtb.atomic(marker, prior_marker)
                self.system("disable", "--now", *LIGHTS, required=False)
                self.restore(current)
                self.restore(own)
                raise
        else:
            self.system("disable", "--now", *LIGHTS)
            marker.unlink(missing_ok=True)
            for name in ("led-boot", "led-shutdown"):
                flag = self.root/"run"/name
                if s2native.read(flag) == "fnos-s2": flag.unlink()
            path = self.state/"light-services.json"
            if path.exists(): self.restore(json.loads(s2dtb.read_regular(path)))
        return {"enabled": enabled}

    def scripts(self, source, directory):
        source, directory = Path(source), Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        legacy = json.loads((source/"bundled/retired-defaults.json").read_text(encoding="utf-8"))
        archive = directory.parent/"scripts-legacy"
        if archive.is_symlink(): raise ValueError("旧脚本备份目录是链接，未迁移任何脚本")
        # Recover archived user edits. Known retired factory files stay in the backup;
        # their dependencies were removed, so never advertise them as runnable tools.
        if archive.is_dir() and not archive.is_symlink():
            for old in archive.iterdir():
                if old.is_symlink() or not old.is_file() or old.suffix not in (".sh", ".run"):
                    continue
                digest = hashlib.sha256(old.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                if legacy.get(old.name) != digest and not (directory/old.name).exists():
                    s2dtb.atomic(directory/old.name, old.read_bytes()); os.chmod(directory/old.name, 0o755)
        for name, digest in legacy.items():
            if Path(name).name != name: raise ValueError("旧脚本清单文件名无效")
            path = directory/name
            if path.is_symlink() or not path.is_file(): continue
            if hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != digest: continue
            archive.mkdir(exist_ok=True)
            destination = archive/name
            if destination.exists() and destination.read_bytes() != path.read_bytes():
                destination = archive/(path.stem + "-" + digest[:10] + path.suffix)
            if not destination.exists():
                s2dtb.atomic(destination, path.read_bytes())
            if destination.read_bytes() == path.read_bytes(): path.unlink()

    def apply_dtb(self, candidate):
        # Validate both the candidate and current board before touching any services.
        s2dtb.validate(Path(candidate).read_bytes())
        s2dtb.board_check(self.root, s2dtb.read_regular(self.root/"boot/fnEnv.txt"))
        planned = self.plan_boot_hooks()
        external = self.external_guard()
        if external["claimed"]:
            if not external["verified"] or not s2dtb.state(self.root)["prepared"]:
                raise ValueError("独立自愈正在管理引导且内容未核验一致，请先按原包检查或交还；未争抢引导")
            if planned:
                original = self.snapshot(["fix-fnenv-shutdown.service"])
                self.remember("retired-shutdown-service.json", original)
                self.stop(original)
                self.retire_boot_hooks(planned)
                self.system("daemon-reload")
            self.system("enable", OWN)
            return {"changed": bool(planned), "external_guard": True, "reboot_required": s2dtb.state(self.root)["reboot_required"],
                    "detail": "DTB 与已核验的独立自愈一致，复用现有守护，不重复接管引导"}
        current = self.boot_services()
        self.remember("boot-services.json", current)
        last_backup = self.state/"last-dtb-backup"
        previous_backup = s2dtb.read_regular(last_backup, optional=True)
        result = None
        retired = []
        try:
            self.stop(current)
            retired = self.retire_boot_hooks(planned)
            if retired: self.system("daemon-reload")
            result = s2dtb.install(candidate, self.root)
            if result.get("backup"):
                s2security.atomic_text(str(last_backup), result["backup"]+"\n", mode=0o600)
            self.system("enable", OWN)
            self.guard_units()
            self.system("enable", "fnos-s2-dtb-guard.service", "fnos-s2-dtb-guard.path", "fnos-s2-scsi-rescan.service")
            self.system("start", "fnos-s2-dtb-guard.path")
            s2dtb.atomic(self.state/"repair-attempts.json", b"[]", mode=0o600)
        except (OSError, ValueError, subprocess.SubprocessError):
            if result and result.get("backup"):
                self.system("disable", "--now", "fnos-s2-dtb-guard.path", "fnos-s2-dtb-guard.service", required=False)
                s2dtb.rollback(result["backup"], self.root)
            self.restore_hooks(retired)
            if previous_backup is None: last_backup.unlink(missing_ok=True)
            else: s2dtb.atomic(last_backup, previous_backup, mode=0o600)
            self.system("daemon-reload", required=False)
            # Do not restart an old auto-rebooting guard in a half-finished migration.
            self.restore(current, start=False)
            raise
        result["detail"] = "两份 DTB 和 fdtfile 已校验写入；请手动安排重启，不会自动重启"
        return result

    def rollback_dtb(self, backup=None):
        if self.external_guard()["claimed"]: raise ValueError("独立自愈仍在管理引导，请先按原包交还后回滚")
        backup = backup or s2native.read(self.state/"last-dtb-backup")
        if not backup: raise ValueError("没有可用的 DTB 升级备份")
        previous_services = {}
        for filename in ("hardware-services.json", "boot-services.json"):
            path = self.state/filename
            if path.exists():
                previous_services[filename] = json.loads(s2dtb.read_regular(path))
                self.validate_states(previous_services[filename])
        own = self.snapshot(["fnos-s2-dtb-guard.path", "fnos-s2-dtb-guard.service", "fnos-s2-scsi-rescan.service"])
        try:
            self.stop(own)
            result = s2dtb.rollback(backup, self.root)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.restore(own)
            raise
        # Current kernel is still using the new DTB. Keep its controller running until reboot,
        # but restore the old enabled states for the next boot without starting GPIO services now.
        self.system("disable", OWN, required=False)
        for states in previous_services.values(): self.restore(states, start=False)
        marker = self.root/"etc/s2hwctl/backend"
        if not marker.is_symlink(): marker.unlink(missing_ok=True)
        result["detail"] = "引导已回滚，旧控制服务将在下次启动恢复；请手动安排重启"
        return result

    def external_guard(self):
        states = self.snapshot(["s2-dtb-guard.path", "s2-dtb-guard.service", "fix-fnenv.service"])
        claimed = any(v.get("ActiveState") in ("active", "activating") or v.get("UnitFileState", "").startswith("enabled") for v in states.values())
        return {"claimed": claimed, **s2dtb.external_reference(self.root)}

    def boot_services(self):
        states = self.snapshot(BOOT)
        markers = {"selfheal.service": "selfheal-check.sh", "selfheal-hourly.service": "selfheal-check.sh",
                   "boot-version-guard.service": "boot-version-guard.sh", "fix-fnenv-shutdown.service": "fix-fnenv"}
        approved = {}
        for unit, state in states.items():
            _, text = self.system("cat", unit, required=False)
            if unit == "selfheal-hourly.timer":
                _, service = self.system("cat", "selfheal-hourly.service", required=False)
                known = "selfheal-check.sh" in service and ("Unit=" not in text or "Unit=selfheal-hourly.service" in text)
            else: known = markers[unit] in text or (unit == "fix-fnenv-shutdown.service" and "led-indicator.sh" in text)
            if known: approved[unit] = state
            elif state.get("ActiveState") == "active" or state.get("UnitFileState", "").startswith("enabled"):
                raise ValueError("发现无法识别的同名引导服务，未停止或改写："+unit)
        return approved

    def guard_units(self):
        names = ["fnos-s2-dtb-guard.service", "fnos-s2-dtb-guard.path", "fnos-s2-scsi-rescan.service"]
        values = {name: s2dtb.read_regular(self.root/"usr/local/lib/fnos-s2/units"/name) for name in names}
        for name, data in values.items(): s2dtb.atomic(self.root/"etc/systemd/system"/name, data)
        self.system("daemon-reload")

    def plan_boot_hooks(self):
        names = ["etc/systemd/system/fix-fnenv-shutdown.service", "etc/systemd/system-shutdown/zz-s2-dtb-guard.sh",
                 "usr/lib/systemd/system-shutdown/zz-s2-dtb-guard.sh", "lib/systemd/system-shutdown/zz-s2-dtb-guard.sh"]
        planned, seen = [], set()
        for name in names:
            path = self.root/name
            if not path.exists() and not path.is_symlink(): continue
            if path.is_symlink(): raise ValueError("旧关机钩子为符号链接，需先人工核实："+name)
            if path.resolve() in seen: continue
            seen.add(path.resolve())
            data = s2dtb.read_regular(path)
            if not any(marker in data for marker in (b"fdtfile", b"fnEnv", b"s2-dtb-guard.sh", b"fix-fnenv.sh", b"led-indicator.sh")):
                raise ValueError("同名关机钩子内容无法识别，未改动："+name)
            planned.append((name, data, path.stat().st_mode & 0o777))
        return planned

    def retire_boot_hooks(self, planned=None):
        if planned is None: planned = self.plan_boot_hooks()
        retired = []
        try:
            for name, data, mode in planned:
                archive = self.state/"retired-boot"/name
                if archive.exists() and archive.read_bytes() != data: raise ValueError("旧钩子备份已存在且不同，拒绝覆盖")
                if not archive.exists(): s2dtb.atomic(archive, data); os.chmod(archive, mode)
                (self.root/name).unlink()
                retired.append((name, data, mode))
        except (OSError, ValueError):
            self.restore_hooks(retired)
            raise
        return retired

    def restore_hooks(self, retired):
        for name, data, mode in retired:
            s2dtb.atomic(self.root/name, data); os.chmod(self.root/name, mode)

    def uninstall(self):
        saved = self.state/"hardware-services.json"
        original = json.loads(s2dtb.read_regular(saved)) if saved.exists() else {}
        self.validate_states(original)
        fan_path = self.state/"fan-original.json"
        fan_original = json.loads(s2dtb.read_regular(fan_path)) if fan_path.exists() else None
        if fan_original is not None and (not isinstance(fan_original,dict) or type(fan_original.get("pwm")) not in (int,float) or not 0 <= fan_original["pwm"] <= 255 or fan_original["pwm"] % 1):
            raise ValueError("原风扇状态记录无效，未停止当前控制器")
        if (self.root/"etc/s2hwctl/led-beautify.enabled").exists(): self.lights(False)
        self.system("disable", "--now", OWN, required=False)
        # Old GPIO/DTB workarounds must not come back on a corrected DTB.
        self.restore({k:v for k,v in original.items() if k in ("led-set.service", "pwm-fancontrol.service")})
        handoff = "system-service"
        if original.get("pwm-fancontrol.service", {}).get("ActiveState") not in ("active", "activating", "reloading"):
            cpu, trip = self.native.cpu()
            status = s2native.kv(self.native.runtime/"status")
            disk_temp = s2native.number(status.get("hdd_max_temp"))
            sampled = s2native.number(status.get("timestamp"))
            disk_safe = sampled is not None and 0 <= self.native.clock()-sampled <= 90 and status.get("hdd_source") == "smartctl" and disk_temp is not None and disk_temp < 50
            if fan_original and cpu is not None and cpu < min(75, trip or 75)-3 and disk_safe:
                node = self.native.fan()
                if node:
                    self.native.write(node/"pwm1", int(fan_original["pwm"]))
                    enable = fan_original.get("enable")
                    if enable in (0,1,2,3) and (node/"pwm1_enable").exists(): self.native.write(node/"pwm1_enable", int(enable))
                handoff = "original-state"
            else: handoff = "safe-full-speed"
        marker = self.root/"etc/s2hwctl/backend"
        if marker.exists() and not marker.is_symlink(): marker.unlink()
        unit = self.root/"etc/systemd/system"/OWN
        if unit.exists() and not unit.is_symlink(): unit.unlink()
        self.system("daemon-reload")
        for path in (fan_path, self.state/"led-original.json"):
            if path.exists() and not path.is_symlink(): path.unlink()
        return {"restored": True, "fan_handoff": handoff, "detail": "硬件控制已交还；温度无法确认安全时保留全速。显式安装的 DTB 与引导守护保留，可独立回滚"}

    def check_jobs(self, directory):
        lock_path = self.root/"run/fnos-s2-bundles.lock"
        if os.name == "posix" and lock_path.exists():
            import fcntl
            with lock_path.open("a+") as stream:
                try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise ValueError("内置模块正在执行，请完成后再卸载")
        for path in (Path(directory)/"jobs").glob("*.pid"):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem) or path.with_suffix(".rc").exists(): continue
            pid = s2native.read(path)
            if not pid.isdigit(): continue
            commandline = self.root/"proc"/pid/"cmdline"
            try: data = commandline.read_bytes()
            except FileNotFoundError: continue
            if b"job_runner.py" in data and path.stem.encode() in data:
                raise ValueError("仍有脚本正在执行，请等待任务结束后卸载，避免中断引导升级")
            if b"/etc/s2hwctl/patches.d/" in data and (b"runner.py" in data or b"module.sh" in data):
                raise ValueError("内置模块正在执行，请完成后再卸载")
        return {"idle": True}

    def remove_data(self, directory, app_name):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", app_name) or app_name in (".", ".."): raise ValueError("应用名无效")
        path = Path(directory)
        if not path.is_absolute(): raise ValueError("应用数据路径必须为绝对路径")
        target = path.resolve()
        parts = [part.casefold() for part in target.parts]
        if len(parts) < 4 or app_name.casefold() not in parts or target.name.casefold() not in (app_name.casefold(), "var"):
            raise ValueError("拒绝删除不属于本应用的目录")
        if not target.is_relative_to(self.root.resolve()): raise ValueError("数据目录超出指定根目录")
        self.check_jobs(target)
        if target.exists(): shutil.rmtree(target)
        return {"removed": True, "shared_data_preserved": True}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "activate", "dtb", "rollback-dtb", "uninstall", "uninstall-check", "remove-data", "scripts", "lights", "lights-off"])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--scripts-dir")
    parser.add_argument("--app-name", default="fnOS-S2")
    args = parser.parse_args()
    if not hasattr(os, "geteuid") or os.geteuid() != 0: parser.error("需要 root")
    try:
        deployment = Deployment()
        if args.action == "install": result = deployment.install(args.path or str(Path(__file__).resolve().parents[1]))
        elif args.action == "activate": result = deployment.activate()
        elif args.action == "dtb": result = deployment.apply_dtb(args.path or str(Path(__file__).with_name(s2dtb.NAME)))
        elif args.action == "rollback-dtb": result = deployment.rollback_dtb(args.path)
        elif args.action in ("lights", "lights-off"): result = deployment.lights(args.action == "lights")
        elif args.action == "uninstall-check":
            if not args.path: parser.error("缺少应用数据目录")
            result = deployment.check_jobs(args.path)
        elif args.action == "remove-data":
            if not args.path: parser.error("缺少应用数据目录")
            result = deployment.remove_data(args.path, args.app_name)
        elif args.action == "scripts":
            if not args.path or not args.scripts_dir: parser.error("缺少源码或脚本目录")
            deployment.scripts(args.path, args.scripts_dir); result = {"installed": True}
        else: result = deployment.uninstall()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as error: parser.exit(1, "部署失败："+str(error)+"\n")
