#!/usr/bin/env python3
"""Weekly schedule calculations shared by the API and systemd execution guard."""
import datetime as dt
import json
import os
import re
import sys
import time


def validate(config):
    if not isinstance(config, dict) or not isinstance(config.get("enabled", False), bool):
        raise ValueError("计划启用状态无效")
    result = {"enabled": config.get("enabled", False)}
    for key in ("shutdown_time", "boot_time"):
        value = config.get(key, "")
        if not isinstance(value, str) or (value and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value)):
            raise ValueError("计划时间应为 HH:MM")
        result[key] = value
    days = config.get("weekdays", list(range(7)))
    if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
        raise ValueError("请至少选择一个有效星期")
    result["weekdays"] = sorted(set(days))
    skips = config.get("skip_dates", [])
    if not isinstance(skips, list) or len(skips) > 64:
        raise ValueError("最多跳过 64 个日期")
    for date in skips:
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("跳过日期应为 YYYY-MM-DD")
        dt.date.fromisoformat(date)
    result["skip_dates"] = sorted(set(skips))
    until = config.get("maintenance_until", 0)
    if type(until) is not int or until < 0:
        raise ValueError("维护截止时间无效")
    result["maintenance_until"] = until
    if result["enabled"] and result["shutdown_time"] and result["shutdown_time"] == result["boot_time"]:
        raise ValueError("开机和关机时间不能相同")
    return result


def read_config(path=None):
    values = {}
    try:
        with open(path or os.environ.get("S2_SCHED_CONF", "/etc/s2-power-schedule.conf"), encoding="utf-8") as stream:
            for line in stream:
                key, sep, value = line.strip().partition("=")
                if sep:
                    values[key] = value
    except FileNotFoundError:
        pass
    return validate({"enabled": values.get("ENABLED") == "1", "shutdown_time": values.get("SHUTDOWN_TIME", ""),
        "boot_time": values.get("BOOT_TIME", ""), "weekdays": [int(v) for v in values.get("WEEKDAYS", "0,1,2,3,4,5,6").split(",")],
        "skip_dates": list(filter(None, values.get("SKIP_DATES", "").split(","))),
        "maintenance_until": int(values.get("MAINTENANCE_UNTIL", "0"))})


def next_event(config, kind, now=None):
    now = time.time() if now is None else now
    time_text = config.get(kind + "_time", "")
    if not config["enabled"] or not time_text:
        return None
    hour, minute = map(int, time_text.split(":"))
    today = dt.datetime.fromtimestamp(now).date()
    for offset in range(370):
        day = today + dt.timedelta(days=offset)
        if day.weekday() not in config["weekdays"] or day.isoformat() in config["skip_dates"]:
            continue
        stamp = dt.datetime.combine(day, dt.time(hour, minute)).timestamp()
        if stamp > max(now, config["maintenance_until"]):
            return {"ts": int(stamp), "local": dt.datetime.fromtimestamp(stamp).isoformat(sep=" "), "kind": kind}
    return None


def due(config, now=None):
    now = time.time() if now is None else now
    current = dt.datetime.fromtimestamp(now)
    return (config["enabled"] and now >= config["maintenance_until"]
            and current.weekday() in config["weekdays"] and current.date().isoformat() not in config["skip_dates"]
            and current.strftime("%H:%M") == config["shutdown_time"])


if __name__ == "__main__":
    try:
        config = read_config()
        if sys.argv[1] == "due":
            sys.exit(0 if due(config) else 1)
        event = next_event(config, sys.argv[2])
        if not event:
            sys.exit(1)
        print(event["ts"])
    except (ValueError, OSError, IndexError) as error:
        print("计划校验失败：" + str(error), file=sys.stderr)
        sys.exit(1)
