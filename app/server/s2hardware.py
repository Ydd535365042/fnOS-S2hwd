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


def _text(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except OSError:
        return None


def _restore(hw, text):
    if text is None:
        if os.path.exists(hw.APP_SCHED_CONF):
            os.unlink(hw.APP_SCHED_CONF)
    else:
        s2security.atomic_text(hw.APP_SCHED_CONF, text)
    hw._KV_CACHE.pop(hw.APP_SCHED_CONF, None)


def _apply_system(hw):
    """Bring systemd and the RTC alarm in line with the saved plan. Returns an error text or ""."""
    enabled = schedule.read_config(hw.APP_SCHED_CONF)["enabled"]
    if hw.NATIVE_ENABLED and boot_unit(hw, enabled):
        return "开机计划服务设置失败"
    if os.path.exists(hw.SCHED_APPLY_BIN) and hw.run(["bash", hw.SCHED_APPLY_BIN, "apply"], timeout=30)[0]:
        return "系统应用失败"
    return ""


# Saving returns as soon as the plan is validated and written. systemd and the RTC are brought in line
# by one background worker: on this board those commands take long enough to make every click feel stuck.
_APPLY_LOCK = threading.Lock()
_APPLY = {"pending": False, "running": False, "error": "", "good": None, "has_good": False}


def _apply_worker(hw):
    while True:
        with _APPLY_LOCK:
            if not _APPLY["pending"]:
                _APPLY["running"] = False
                return
            _APPLY["pending"] = False
        applied = _text(hw.APP_SCHED_CONF)
        try:
            error = _apply_system(hw)
        except (OSError, ValueError, TypeError) as failure:
            error = str(failure) or "系统应用失败"
        with _APPLY_LOCK:
            if not error:
                _APPLY.update(error="", good=applied, has_good=True)
        if error:
            # Put back the last plan the system accepted, so the page never shows a plan that is not in force.
            # A newer save made meanwhile wins instead: the next round applies it.
            restored = False
            with LOCK:
                with _APPLY_LOCK:
                    newer = _APPLY["pending"]
                    back = _APPLY["has_good"] and _APPLY["good"] != applied
                    good = _APPLY["good"]
                if not newer and back:
                    try:
                        _restore(hw, good)
                        restored = True
                    except OSError:
                        restored = False
            if not newer:
                settled = False
                if restored:
                    try:
                        settled = not _apply_system(hw)
                    except (OSError, ValueError, TypeError):
                        settled = False
                tail = "，已恢复为上一次生效的计划" if settled else "，已退回上一次的计划，但系统服务仍未就绪" if restored else "，请检查计划服务与 RTC"
                with _APPLY_LOCK:
                    _APPLY["error"] = "计划应用失败（%s）" % error + tail
        hw.bust_services_cache()


def apply_state():
    with _APPLY_LOCK:
        return {"applying": _APPLY["running"] or _APPLY["pending"], "apply_error": _APPLY["error"]}


def set_schedule(hw, body):
    current = schedule.read_config(hw.APP_SCHED_CONF)
    config = schedule.validate({**current, **body})
    helper = os.environ.get("S2_SCHEDULE_HELPER", "/usr/local/sbin/s2_schedule.py")
    if (config["weekdays"] != list(range(7)) or config["skip_dates"] or config["maintenance_until"]) and not os.path.isfile(helper):
        return False, "周计划执行组件尚未部署，请先部署新版硬件配置"
    saved = _text(hw.APP_SCHED_CONF)
    hw.write_kv(hw.APP_SCHED_CONF, {"ENABLED": "1" if config["enabled"] else "0",
        "SHUTDOWN_TIME": config["shutdown_time"], "BOOT_TIME": config["boot_time"],
        "WEEKDAYS": ",".join(map(str, config["weekdays"])), "SKIP_DATES": ",".join(config["skip_dates"]),
        "MAINTENANCE_UNTIL": str(config["maintenance_until"])})
    with _APPLY_LOCK:
        if not _APPLY["has_good"]:             # first save since start: what was on disk is what the system runs
            _APPLY.update(good=saved, has_good=True)
        _APPLY.update(pending=True, error="")
        start = not _APPLY["running"]
        if start:
            _APPLY["running"] = True
    if start:
        threading.Thread(target=_apply_worker, args=(hw,), name="s2-schedule-apply", daemon=True).start()
    return True, "计划已保存" + ("；当前未检测到 RTC 唤醒" if config["boot_time"] and not hw.rtc_wake_supported() else "")


def dispatch(hw, operation, body):
    try:
        if operation == "schedule_info":
            # Read-only: never wait behind a write holding LOCK.
            config = schedule.read_config(hw.APP_SCHED_CONF)
            return True, {**config, **apply_state(), "rtc_wake": hw.rtc_wake_supported(),
                "next_shutdown": schedule.next_event(config, "shutdown"), "next_boot": schedule.next_event(config, "boot"),
                "timezone": time.strftime("%Z")}
        with LOCK:
            if operation == "set_power_schedule":
                return set_schedule(hw, body)
    except (ValueError, TypeError, OSError) as error:
        return False, str(error)
    return False, "未知硬件配置操作"
