"""Strict hardware configuration bundles and extended policy operations."""
import glob
import importlib.util
import hashlib
import json
import os
import threading
import time

import s2security
from s2validation import integer

_path = os.path.join(os.path.dirname(__file__), "..", "firmware", "s2_schedule.py")
_spec = importlib.util.spec_from_file_location("s2_schedule", _path)
schedule = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(schedule)
LOCK = threading.RLock()
# Only operations the console still exposes; the old operations-center bundle/guard ops were removed.
OPS = {"schedule_info"}


def boot_unit(hw, enabled):
    """Enable or disable the boot-time unit only when its state has to change.
    `systemctl enable` reloads every unit (a second or more on this board); --no-reload skips that,
    the symlink only matters at the next boot."""
    current = bool(glob.glob("/etc/systemd/system/*.wants/s2-power-schedule.service"))
    if current == enabled:
        return 0
    return hw.run(["systemctl", "enable" if enabled else "disable", "--no-reload", "s2-power-schedule.service"], timeout=30)[0]


def set_schedule(hw, body):
    current = schedule.read_config(hw.APP_SCHED_CONF)
    config = schedule.validate({**current, **body})
    helper = os.environ.get("S2_SCHEDULE_HELPER", "/usr/local/sbin/s2_schedule.py")
    if (config["weekdays"] != list(range(7)) or config["skip_dates"] or config["maintenance_until"]) and not os.path.isfile(helper):
        return False, "周计划执行组件尚未部署，请先部署新版硬件配置"
    saved = None
    if os.path.isfile(hw.APP_SCHED_CONF):
        with open(hw.APP_SCHED_CONF, encoding="utf-8") as stream:
            saved = stream.read()
    try:
        hw.write_kv(hw.APP_SCHED_CONF, {"ENABLED": "1" if config["enabled"] else "0",
            "SHUTDOWN_TIME": config["shutdown_time"], "BOOT_TIME": config["boot_time"],
            "WEEKDAYS": ",".join(map(str, config["weekdays"])), "SKIP_DATES": ",".join(config["skip_dates"]),
            "MAINTENANCE_UNTIL": str(config["maintenance_until"])})
        if hw.NATIVE_ENABLED:
            if boot_unit(hw, config["enabled"]):
                raise ValueError("开机计划服务设置失败")
        if os.path.exists(hw.SCHED_APPLY_BIN):
            rc, _ = hw.run(["bash", hw.SCHED_APPLY_BIN, "apply"], timeout=30)
            if rc:
                raise ValueError("系统应用失败")
    except (OSError, ValueError):
        try:
            if saved is None:
                if os.path.exists(hw.APP_SCHED_CONF):
                    os.unlink(hw.APP_SCHED_CONF)
            else:
                s2security.atomic_text(hw.APP_SCHED_CONF, saved)
            hw._KV_CACHE.pop(hw.APP_SCHED_CONF, None)
        except OSError:
            return False, "计划应用失败，原配置恢复失败，请检查配置目录权限和计划服务"
        restored = True
        if hw.NATIVE_ENABLED:
            restored = restored and boot_unit(hw, current["enabled"]) == 0
        if os.path.exists(hw.SCHED_APPLY_BIN):
            rc, _ = hw.run(["bash", hw.SCHED_APPLY_BIN, "apply"], timeout=30)
            restored = restored and rc == 0
        return False, "计划应用失败，已恢复原配置" + ("" if restored else "；系统服务恢复仍失败，请检查计划服务与 RTC")
    return True, "计划已保存" + ("；当前未检测到 RTC 唤醒" if config["boot_time"] and not hw.rtc_wake_supported() else "")


def dispatch(hw, operation, body):
    try:
        if operation == "schedule_info":
            # Read-only: never wait behind a write holding LOCK (schedule apply can take 30 s).
            config = schedule.read_config(hw.APP_SCHED_CONF)
            return True, {**config, "rtc_wake": hw.rtc_wake_supported(),
                "next_shutdown": schedule.next_event(config, "shutdown"), "next_boot": schedule.next_event(config, "boot"),
                "timezone": time.strftime("%Z")}
        with LOCK:
            if operation == "set_power_schedule":
                return set_schedule(hw, body)
    except (ValueError, TypeError, OSError) as error:
        return False, str(error)
    return False, "未知硬件配置操作"
