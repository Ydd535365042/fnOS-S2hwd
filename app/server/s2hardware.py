"""Strict hardware configuration bundles and extended policy operations."""
import importlib.util
import hashlib
import json
import os
import threading
import time

import s2security
from s2store import integer

_path = os.path.join(os.path.dirname(__file__), "..", "firmware", "s2_schedule.py")
_spec = importlib.util.spec_from_file_location("s2_schedule", _path)
schedule = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(schedule)
LOCK = threading.RLock()
OPS = {"config_snapshot", "config_validate", "config_apply", "schedule_info", "set_maintenance", "fan_guard_info", "set_fan_guard", "diagnostics_files"}


def guard_path(hw):
    return os.path.join(hw.APP_CONF_DIR, "fan-guard.conf")


def guard(hw):
    values = hw.read_kv(guard_path(hw))
    status = hw.read_kv(hw.FAN_STATUS)
    return {"enabled": values.get("ENABLED") == "1", "cpu_limit": int(values.get("CPU_LIMIT", "80")),
            "disk_limit": int(values.get("DISK_LIMIT", "55")), "fail_samples": int(values.get("FAIL_SAMPLES", "3")),
            "active": status.get("mode") == "protect", "reason": status.get("guard_reason", "")}


def validate_guard(body):
    if not isinstance(body, dict) or not isinstance(body.get("enabled", False), bool):
        raise ValueError("风扇保护启用状态无效")
    return {"enabled": body.get("enabled", False), "cpu_limit": integer(body.get("cpu_limit", 80), 50, 85),
            "disk_limit": integer(body.get("disk_limit", 55), 40, 60), "fail_samples": integer(body.get("fail_samples", 3), 1, 10)}


def snapshot(hw):
    values = hw.read_kv(hw.ETC_FAN)
    fan = {"tempProfile": values.get("TEMP_PROFILE", "quiet")}
    for name, defaults in (("hdd", [42, 48, 52, 55, 30, 70]), ("cpu", [60, 65, 72, 80, 30, 70])):
        fan[name] = {key: int(values.get(name.upper() + "_" + key.upper(), default))
                     for key, default in zip(("t1", "t2", "t3", "t4", "d2", "d3"), defaults)}
    led, beep = hw.read_kv(hw.LED_OVERRIDE_CONF), hw.load_app_conf(hw.APP_BEEP_CONF)
    result = {"fan": fan, "led": {"power": led.get("POWER_LED", "auto"), "sata": led.get("SATA_LED", "auto")},
              "beep": {"ms": int(beep.get("beep_ms", 280)), "boot_beep": bool(beep.get("boot_beep", True))},
              "schedule": schedule.read_config(hw.APP_SCHED_CONF), "guard": validate_guard(guard(hw))}
    # Maintenance is temporary, and must not be resurrected by old backups.
    result["schedule"]["maintenance_until"] = 0
    return result


def validate_bundle(config):
    if not isinstance(config, dict) or set(config) != {"fan", "led", "beep", "schedule", "guard"}:
        raise ValueError("备份必须包含风扇、灯光、蜂鸣、计划和保护配置")
    fan = config["fan"]
    if not isinstance(fan, dict) or fan.get("tempProfile") not in ("quiet", "performance", "custom"):
        raise ValueError("风扇配置无效")
    for kind, low, high in (("hdd", 30, 55), ("cpu", 40, 80)):
        curve = fan.get(kind)
        if not isinstance(curve, dict) or set(curve) != {"t1", "t2", "t3", "t4", "d2", "d3"}:
            raise ValueError("风扇曲线格式无效")
        temperatures = [integer(curve[key], low, high) for key in ("t1", "t2", "t3", "t4")]
        if any(a >= b for a, b in zip(temperatures, temperatures[1:])):
            raise ValueError("风扇温度必须严格递增")
        if integer(curve["d2"], 30, 100) > integer(curve["d3"], 30, 100):
            raise ValueError("风扇转速必须递增")
    led, beep = config["led"], config["beep"]
    if not isinstance(led, dict) or led.get("power") not in ("auto", "off") or led.get("sata") not in ("auto", "off", "breathe"):
        raise ValueError("灯光配置无效")
    if not isinstance(beep, dict) or not isinstance(beep.get("boot_beep"), bool):
        raise ValueError("蜂鸣配置无效")
    integer(beep.get("ms"), 30, 5000)
    schedule.validate(config["schedule"])
    validate_guard(config["guard"])
    return config


def set_schedule(hw, body):
    current = schedule.read_config(hw.APP_SCHED_CONF)
    config = schedule.validate({**current, **body})
    helper = os.environ.get("S2_SCHEDULE_HELPER", "/usr/local/sbin/s2_schedule.py")
    if (config["weekdays"] != list(range(7)) or config["skip_dates"] or config["maintenance_until"]) and not os.path.isfile(helper):
        return False, "周计划执行组件尚未部署，请先部署新版硬件配置"
    hw.write_kv(hw.APP_SCHED_CONF, {"ENABLED": "1" if config["enabled"] else "0",
        "SHUTDOWN_TIME": config["shutdown_time"], "BOOT_TIME": config["boot_time"],
        "WEEKDAYS": ",".join(map(str, config["weekdays"])), "SKIP_DATES": ",".join(config["skip_dates"]),
        "MAINTENANCE_UNTIL": str(config["maintenance_until"])})
    if os.path.exists(hw.SCHED_APPLY_BIN):
        rc, output = hw.run(["bash", hw.SCHED_APPLY_BIN, "apply"], timeout=30)
        if rc:
            return False, "计划已保存，但系统应用失败，请检查计划服务"
    return True, "计划已保存" + ("；当前未检测到 RTC 唤醒" if config["boot_time"] and not hw.rtc_wake_supported() else "")


def dispatch(hw, operation, body):
    try:
        with LOCK:
            if operation == "diagnostics_files":
                return True, [{"id": key, "title": title, "ok": os.path.isfile(path) and os.access(path, os.R_OK),
                    "detail": "配置可读" if os.path.isfile(path) and os.access(path, os.R_OK) else "未部署配置或读取权限不足，请部署硬件配置", "repair": ""}
                    for key, title, path in (("fan-conf", "风扇配置文件", hw.ETC_FAN), ("schedule-conf", "定时计划配置文件", hw.APP_SCHED_CONF))]
            if operation == "config_snapshot":
                return True, snapshot(hw)
            if operation == "config_validate":
                validate_bundle(body.get("config"))
                return True, {"valid": True}
            if operation == "schedule_info":
                config = schedule.read_config(hw.APP_SCHED_CONF)
                return True, {**config, "rtc_wake": hw.rtc_wake_supported(),
                    "next_shutdown": schedule.next_event(config, "shutdown"), "next_boot": schedule.next_event(config, "boot"),
                    "timezone": time.strftime("%Z")}
            if operation == "set_maintenance":
                until = integer(body.get("until", 0), 0, int(time.time()) + 86460)
                return set_schedule(hw, {"maintenance_until": until})
            if operation == "set_power_schedule":
                return set_schedule(hw, body)
            if operation == "fan_guard_info":
                return True, guard(hw)
            if operation == "set_fan_guard":
                config = validate_guard(body)
                if config["enabled"] and not hw._fan_script_has("GUARD_CONF"):
                    return False, "设备风扇程序尚未支持保护策略，请先部署新版硬件配置"
                hw.write_kv(guard_path(hw), {"ENABLED": "1" if config["enabled"] else "0",
                    "CPU_LIMIT": str(config["cpu_limit"]), "DISK_LIMIT": str(config["disk_limit"]),
                    "FAIL_SAMPLES": str(config["fail_samples"])})
                return True, "风扇保护已保存"
            if operation == "config_apply":
                if body.get("expected"):
                    current = json.dumps(snapshot(hw), ensure_ascii=False, sort_keys=True, indent=2)
                    if hashlib.sha256(current.encode()).hexdigest() != body["expected"]:
                        return False, "配置已被其他操作修改，请重新预览差异"
                config = validate_bundle(body.get("config"))
                config["schedule"]["maintenance_until"] = schedule.read_config(hw.APP_SCHED_CONF)["maintenance_until"]
                paths = [hw.ETC_FAN, hw.LED_OVERRIDE_CONF, hw.APP_BEEP_CONF, hw.APP_SCHED_CONF, guard_path(hw)]
                saved = {}
                for path in paths:
                    if os.path.isfile(path):
                        with open(path, encoding="utf-8") as stream:
                            saved[path] = stream.read()
                    else:
                        saved[path] = None
                try:
                    for op, key in (("set_fan", "fan"), ("set_led_override", "led"), ("set_beep", "beep"), ("set_power_schedule", "schedule"), ("set_fan_guard", "guard")):
                        ok, result = hw.dispatch(op, config[key])
                        if not ok:
                            raise ValueError(str(result))
                except (OSError, ValueError):
                    for path, content in saved.items():
                        if content is not None:
                            s2security.atomic_text(path, content)
                        elif os.path.isfile(path):
                            os.unlink(path)
                    hw._KV_CACHE.clear()
                    hw.restart_service("fan-control")
                    hw.restart_service("power-led-net")
                    if os.path.exists(hw.SCHED_APPLY_BIN):
                        hw.run(["bash", hw.SCHED_APPLY_BIN, "apply"], timeout=30)
                    return False, "恢复失败，已回滚配置文件；请检查服务状态"
                return True, "配置已恢复"
    except (ValueError, TypeError, OSError) as error:
        return False, str(error)
    return False, "未知硬件配置操作"
