#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""硬件告警：后台线程每 CHECK_SEC 秒查一次，问题出现记一条、恢复再记一条，存进 var/alerts.json。

不依赖网页开着：控制台服务在跑就在查。网页只是读这份记录（/api/alerts）并标已读。

规则（阈值与概览页仪表的黄 / 红分界一致，用户在两处看到的是同一把尺子）：
  处理器温度   ≥70℃ 警告  ≥85℃ 严重
  硬盘温度     ≥50℃ 警告  ≥60℃ 严重          每块盘单独判；休眠盘不判（读到的是旧值）
  硬盘容量     ≥85% 警告  ≥95% 严重          按每块盘的已挂载容量（阵列共享的卷只报一次）
  硬盘健康     SMART 警告 / 故障
  内存占用     ≥92% 严重                      连续 2 次才报，瞬时尖峰不算
  硬件服务     状态 failed
  风扇         100% 持续 ≥5 分钟              说明散热跟不上，不是单纯在全速测试

去抖：同一个问题（key）只在「从正常变成异常」或「警告升级成严重」时记一条，持续期间不重复刷屏；
回到正常时记一条「已恢复」。降一级（严重 → 警告）不另记，免得在阈值附近来回抖。
温度类有 3℃ 回差：到 70℃ 报警告，要降到 67℃ 以下才算恢复。

开销：一次检查只读 /proc、/sys 和 statvfs（collect_sensor 那一套，0 个子进程），
SMART 与服务状态复用 hwinfo / hwctl 已有的缓存（10 分钟 / 15 秒），不会额外唤醒休眠盘。
"""
import json
import os
import threading
import time

CHECK_SEC = int(os.environ.get("S2_ALERT_SEC", "30"))
MAX_EVENTS = 200
FAN_FULL_SEC = int(os.environ.get("S2_ALERT_FAN_SEC", "300"))
HYST = 3          # 温度回差（℃）
PCT_HYST = 2      # 容量 / 内存回差（百分点）

RULES = {
    "cpu_temp": (70, 85),
    "disk_temp": (50, 60),
    "disk_usage": (85, 95),
    "mem": (None, 92),
}

SVC_ZH = {
    "fan-control": "风扇控制", "sata-led-manager": "硬盘指示灯管理", "power-led-net": "电源网络指示灯管理",
    "power-key-daemon": "电源按键服务", "random-mac": "网卡物理地址", "set-hostname": "设置主机名",
    "selfheal": "自我修复服务", "boot-version-guard": "启动版本保护", "s2-power-schedule": "定时开关机服务",
}

_LEVEL_RANK = {"": 0, "warn": 1, "crit": 2}


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_t(v):
    return ("%d" % v) if float(v).is_integer() else ("%.1f" % v)


def _level(v, warn, crit, prev, hyst):
    """当前值 → "" / warn / crit。已经在告警状态时用回差：降到阈值 - hyst 以下才算回落。"""
    if v is None:
        return prev            # 读不到：保持原状态，既不误报也不误报恢复
    if crit is not None and (v >= crit or (prev == "crit" and v >= crit - hyst)):
        return "crit"
    if warn is not None and (v >= warn or (prev in ("warn", "crit") and v >= warn - hyst)):
        return "warn"
    return ""


def disk_label(d, idx_map):
    """跟页面同一套叫法：系统盘 / 盘位 N 的硬盘 / 硬盘 N。"""
    name = d.get("name", "")
    slot = d.get("slot_name") or ""
    if (d.get("role") or "") == "系统盘":
        return "系统盘"
    base = "硬盘 %d" % idx_map.get(name, 0) if idx_map.get(name) else name
    return ("%s（%s）" % (base, slot)) if slot and slot.startswith("盘位") else base


class AlertStore:
    """事件记录 + 当前活动问题。线程安全；落盘用 tmp + replace，断电不会留半截 JSON。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.events = []         # 新的在前
        self.active = {}         # key -> {"level", "since", "title"}
        self.seq = 0
        self._streak = {}        # key -> 连续命中次数（内存那条用）
        self._fan_full_since = None
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.events = [e for e in d.get("events", []) if isinstance(e, dict)][:MAX_EVENTS]
            self.active = {k: v for k, v in (d.get("active") or {}).items() if isinstance(v, dict)}
            self.seq = int(d.get("seq") or 0)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"seq": self.seq, "events": self.events, "active": self.active}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---------- 记录 ----------
    def _push(self, key, level, cat, title, detail, now):
        self.seq += 1
        self.events.insert(0, {"id": self.seq, "ts": int(now), "key": key, "level": level, "cat": cat,
                               "title": title, "detail": detail, "read": False})
        del self.events[MAX_EVENTS:]

    def report(self, findings, now=None):
        """findings: {key: (level, cat, title, detail)}，level 为 "" 表示这个 key 当前正常。
        只出现过的 key 才会被判恢复 —— 某一类这次没采到（比如 SMART 还没刷新），不会被当成全部恢复。"""
        now = time.time() if now is None else now
        changed = False
        with self.lock:
            for key, (level, cat, title, detail) in findings.items():
                prev = self.active.get(key, {}).get("level", "")
                if level and _LEVEL_RANK[level] > _LEVEL_RANK[prev]:
                    self._push(key, level, cat, title, detail, now)
                    # since 记的是「这次问题开始」的时刻：警告升级成严重不重置，恢复时算的是整段时长
                    since = self.active.get(key, {}).get("since", int(now))
                    self.active[key] = {"level": level, "since": since, "title": title}
                    changed = True
                elif level and prev:
                    if level != prev:                        # 严重 → 警告：只改状态，不另记
                        self.active[key]["level"] = level
                        changed = True
                elif not level and prev:
                    since = self.active[key].get("since", now)
                    old = self.active.pop(key)
                    mins = max(1, int((now - since) / 60))
                    self._push(key, "ok", cat, old.get("title", title).split("：")[0] + "已恢复正常",
                               "持续约 %d 分钟" % mins if mins < 120 else "持续约 %.1f 小时" % (mins / 60.0), now)
                    changed = True
            if changed:
                self._save()
        return changed

    # ---------- 网页用 ----------
    def snapshot(self, limit=100):
        with self.lock:
            ev = [dict(e) for e in self.events[:limit]]
            unread = sum(1 for e in self.events if not e.get("read"))
            unread_crit = sum(1 for e in self.events if not e.get("read") and e.get("level") == "crit")
            active = [{"key": k, **v} for k, v in self.active.items()]
        active.sort(key=lambda a: (-_LEVEL_RANK.get(a.get("level", ""), 0), -a.get("since", 0)))
        return {"events": ev, "unread": unread, "unread_crit": unread_crit, "active": active,
                "last_id": ev[0]["id"] if ev else 0, "check_sec": CHECK_SEC}

    def mark_read(self, ids=None):
        with self.lock:
            n = 0
            for e in self.events:
                if not e.get("read") and (ids is None or e.get("id") in ids):
                    e["read"] = True
                    n += 1
            if n:
                self._save()
        return n

    def clear(self):
        """清空历史记录。仍在持续的问题保留（它们还没恢复），只是旧的条目不再显示。"""
        with self.lock:
            self.events = []
            self._save()


# ---------------- 规则 ----------------

def evaluate(store, sensor, hw, services, now=None):
    """一次检查：把采到的数据变成 findings 交给 store.report()。拆出来便于单元测试（不碰硬件）。"""
    now = time.time() if now is None else now
    f = {}
    act = store.active

    def prev(key):
        return act.get(key, {}).get("level", "")

    # 处理器温度
    t = _num((sensor or {}).get("cpu_temp"))
    if t is not None:
        w, c = RULES["cpu_temp"]
        lv = _level(t, w, c, prev("cpu_temp"), HYST)
        f["cpu_temp"] = (lv, "temp", "处理器温度过高：%s℃" % _fmt_t(t),
                         "超过 %d℃%s" % (c if lv == "crit" else w, "，可能触发降频" if lv == "crit" else ""))

    disks = (hw or {}).get("disks") or []
    live = {d.get("name"): d for d in (sensor or {}).get("disk_list") or [] if isinstance(d, dict)}
    # 页面上的「硬盘 N」编号：跳过系统盘，按名字顺序
    idx, n = {}, 0
    for d in sorted(disks, key=lambda x: x.get("name", "")):
        if (d.get("role") or "") != "系统盘":
            n += 1
            idx[d.get("name")] = n
    seen_vol = set()
    for d in disks:
        name = d.get("name") or ""
        if not name:
            continue
        lab = disk_label(d, idx)
        ld = live.get(name) or {}
        standby = ld.get("standby") if ld.get("standby") is not None else d.get("standby")
        # 硬盘温度：休眠盘读到的是睡前的旧值，不判
        if not standby:
            dt = _num(ld.get("temp"))
            if dt is None and isinstance(d.get("smart"), dict):
                dt = _num(d["smart"].get("temp"))
            if dt is not None:
                w, c = RULES["disk_temp"]
                lv = _level(dt, w, c, prev("disk_temp:" + name), HYST)
                f["disk_temp:" + name] = (lv, "temp", "%s温度过高：%s℃" % (lab, _fmt_t(dt)),
                                          "超过 %d℃，长期高温会缩短硬盘寿命" % (c if lv == "crit" else w))
        # 容量：同一个阵列卷只报一次（挂在第一块成员盘名下）
        u = d.get("usage") or None
        if isinstance(u, dict) and _num(u.get("pct")) is not None:
            vol = tuple(sorted(u.get("mounts") or []))
            key = "disk_usage:" + (",".join(vol) if u.get("shared") and vol else name)
            if key not in seen_vol:
                seen_vol.add(key)
                p = _num(u.get("pct"))
                w, c = RULES["disk_usage"]
                lv = _level(p, w, c, prev(key), PCT_HYST)
                free = _num(u.get("free"))
                where = ("阵列卷 " + "、".join(vol)) if u.get("shared") and vol else lab
                f[key] = (lv, "disk", "%s空间不足：已用 %.1f%%" % (where, p),
                          ("剩余 %s" % _fmt_bytes(free) if free is not None else "") +
                          ("，写满后可能无法写入数据" if lv == "crit" else ""))
        # SMART 健康：只在这次真的读到了 SMART 时判（没读到不等于恢复）
        sm = d.get("smart")
        if isinstance(sm, dict) and sm.get("health") in ("ok", "warn", "fail"):
            h = sm["health"]
            lv = "crit" if h == "fail" else "warn" if h == "warn" else ""
            bits = []
            for k, zh in (("reallocated", "重映射扇区"), ("pending", "待映射扇区"), ("uncorrectable", "不可纠正错误")):
                v = _num(sm.get(k))
                if v:
                    bits.append("%s %d" % (zh, v))
            f["disk_health:" + name] = (lv, "disk", "%s健康%s" % (lab, "检测失败" if h == "fail" else "出现警告"),
                                        ("、".join(bits) + "，" if bits else "") + "建议尽快备份数据")

    # 内存：连续 2 次 ≥92% 才报
    mem = (sensor or {}).get("mem") or {}
    mp = _num(mem.get("used_pct"))
    if mp is not None:
        _, c = RULES["mem"]
        raw = _level(mp, None, c, prev("mem"), PCT_HYST)
        s = store._streak.get("mem", 0) + 1 if raw else 0
        store._streak["mem"] = s
        lv = raw if (s >= 2 or prev("mem")) else ""
        f["mem"] = (lv, "mem", "内存占用过高：%.0f%%" % mp, "可用内存不足，程序可能变慢或被系统结束")

    # 硬件服务
    for svc, st in (services or {}).items():
        if st in ("unknown", "", None):
            continue
        zh = SVC_ZH.get(svc, svc)
        f["svc:" + svc] = ("crit" if st == "failed" else "", "svc", "%s服务异常停止" % zh,
                           "可以在控制页「服务状态」重启，或运行自检脚本")

    # 风扇全速持续
    duty = _num((sensor or {}).get("fan_duty"))
    if duty is not None:
        if duty >= 100 and (sensor or {}).get("fan_mode") != "test":
            if store._fan_full_since is None:
                store._fan_full_since = now
        else:
            store._fan_full_since = None
        long_full = store._fan_full_since is not None and now - store._fan_full_since >= FAN_FULL_SEC
        f["fan_full"] = ("warn" if long_full else "", "fan", "风扇持续全速运行",
                         "已全速超过 %d 分钟，请检查机箱通风和灰尘" % max(1, FAN_FULL_SEC // 60))

    store.report(f, now)
    return f


def _fmt_bytes(b):
    if b is None:
        return "—"
    v, i = float(b), 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    while abs(v) >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return ("%.1f %s" % (v, units[i])) if i >= 3 else ("%d %s" % (v, units[i]))


class Watcher(threading.Thread):
    """后台检查线程。collect 是一个返回 (sensor, hwinfo, services) 的函数，由 server.py 注入，
    这样这里不用关心「直控还是经代理」。"""

    def __init__(self, store, collect, interval=CHECK_SEC):
        super().__init__(daemon=True, name="s2-alerts")
        self.store, self.collect, self.interval = store, collect, max(5, interval)
        self.stop_ev = threading.Event()
        self.last_ok = 0.0
        self.last_err = ""

    def run(self):
        # 开机先等一会：服务刚起、硬盘刚通电时温度和服务状态还没稳定
        if self.stop_ev.wait(min(20, self.interval)):
            return
        while not self.stop_ev.is_set():
            try:
                sensor, hw, services = self.collect()
                evaluate(self.store, sensor, hw, services)
                self.last_ok, self.last_err = time.time(), ""
            except Exception as e:  # noqa
                self.last_err = str(e)
            self.stop_ev.wait(self.interval)
