"""Operations features, using bounded local storage and injectable hardware/network calls."""
import concurrent.futures
import csv
import datetime
import difflib
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import secrets
import smtplib
import socket
import ssl
import sqlite3
import threading
import time
import urllib.parse
from email.message import EmailMessage

from s2store import Problem, Store, integer


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise Problem("地址无效")
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError:
        raise Problem("地址需为不含账号、密码或片段的 HTTP / HTTPS URL") from None
    return parsed


def check_address(address):
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise Problem("不允许回环、链路本地或系统保留网络地址")


def network_json(url, headers=None, body=None):
    """No redirects; verify actual peer before sending credentials (including DNS rebinding)."""
    parsed = endpoint(url)
    conn_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = conn_type(parsed.hostname, parsed.port, timeout=6)
    try:
        connection.connect()
        check_address(connection.sock.getpeername()[0])
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        content = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request_headers = {"Accept": "application/json", **(headers or {})}
        if content is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request("GET" if body is None else "POST", path, content, request_headers)
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or not 200 <= response.status < 300:
            raise Problem("远程服务响应失败：HTTP %s" % response.status, 502)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            if body is not None:
                return {"accepted": True}
            raise Problem("远程服务未返回 JSON", 502) from None
    finally:
        connection.close()


class Operations:
    def __init__(self, directory, hardware, sensor, alert_snapshot, clock=time.time, network=network_json):
        self.store = Store(directory)
        self.hardware, self.sensor, self.alert_snapshot = hardware, sensor, alert_snapshot
        self.clock, self.network = clock, network
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.last_smart = 0
        self.last_error = ""
        self.last_sample = 0
        self.fleet_lock = threading.Lock()
        self.fleet_cache = None
        self.fleet_at = 0
        self.history_enabled = self.store.get("history_enabled", False) is True
        self.notifications_enabled = bool(self.store.get("notifications", {}).get("enabled"))

    def call(self, operation, params=None):
        result = self.hardware(operation, params or {})
        if not result.get("ok"):
            raise Problem(result.get("msg") or "设备操作失败", 502)
        return result.get("data")

    def configuration(self):
        return self.call("config_snapshot")

    def version(self, label, actor, config=None):
        config = self.configuration() if config is None else config
        raw = json.dumps(config, ensure_ascii=False, sort_keys=True)
        with self.store.db() as db:
            row = db.execute("SELECT config FROM versions ORDER BY id DESC LIMIT 1").fetchone()
            if row and row[0] == raw and label == "自动保存":
                return
            cur = db.execute("INSERT INTO versions(ts,actor,label,config) VALUES (?,?,?,?)", (self.clock(), actor, label[:120], raw))
            db.execute("DELETE FROM versions WHERE id <= (SELECT max(id)-200 FROM versions)")
            return cur.lastrowid

    def backup(self, label, actor):
        config = self.configuration()
        identifier = self.version(label or "手动备份", actor, config)
        return {"id": identifier, "format": "s2-config-v1", "created": self.clock(), "config": config}

    def restore_preview(self, body):
        config = body.get("config")
        if body.get("id") is not None:
            identifier = integer(body["id"], 1, 2147483647)
            with self.store.db() as db:
                row = db.execute("SELECT config FROM versions WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise Problem("配置版本不存在", 404)
            config = json.loads(row[0])
        if not isinstance(config, dict) or len(json.dumps(config)) > 65536:
            raise Problem("备份格式无效或过大")
        self.call("config_validate", {"config": config})
        current = self.configuration()
        before, after = [json.dumps(c, ensure_ascii=False, sort_keys=True, indent=2) for c in (current, config)]
        checksum = hashlib.sha256((before + "\n" + after).encode()).hexdigest()
        return {"config": config, "digest": checksum, "expected": hashlib.sha256(before.encode()).hexdigest(),
                "diff": "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile="当前配置", tofile="待恢复配置", lineterm=""))}

    def restore(self, body, actor):
        with self.lock:
            preview = self.restore_preview(body)
            if not secrets.compare_digest(str(body.get("digest", "")), preview["digest"]):
                raise Problem("配置已变化，请重新预览并确认", 409)
            self.version("恢复前自动备份", actor)
            self.call("config_apply", {"config": preview["config"], "expected": preview["expected"]})
            self.version("恢复配置", actor)
        return {"restored": True}

    def maintenance(self):
        config = self.call("schedule_info") or {}
        until = int(config.get("maintenance_until", 0))
        return {"active": until > self.clock(), "until": until,
                "reason": self.store.get("maintenance_reason", "") if until > self.clock() else ""}

    def set_maintenance(self, body):
        minutes = integer(body.get("minutes", 0), 0, 1440)
        until = int(self.clock()) + minutes * 60 if minutes else 0
        self.call("set_maintenance", {"until": until})
        self.store.set("maintenance_reason", str(body.get("reason", ""))[:200])
        return self.maintenance()

    def sample(self):
        with self.lock:
            if self.history_enabled:
                self.sample_history()
        if self.notifications_enabled:
            self.enqueue_alerts()
            self.deliver_pending()

    def sample_history(self):
        now = self.clock()
        sensor = self.sensor()
        metrics = {k: number(sensor.get(k)) for k in ("cpu_temp", "hdd_max_temp", "fan_duty", "cpu_pct")}
        metrics["mem_pct"] = number((sensor.get("mem") or {}).get("used_pct"))
        net = sensor.get("net") or {}
        metrics.update(rx_bps=number(net.get("rx_bps")), tx_bps=number(net.get("tx_bps")))
        days = self.store.get("retention_days", 30)
        with self.store.db() as db:
            db.execute("INSERT INTO samples(ts,data) VALUES (?,?)", (now, json.dumps(metrics)))
            db.execute("DELETE FROM samples WHERE ts<?", (now - days * 86400,))
            db.execute("DELETE FROM disks WHERE ts<?", (now - days * 86400,))
        if now - self.last_smart >= 600:
            info = self.call("hwinfo") or {}
            with self.store.db() as db:
                for disk in info.get("disks", []):
                    if disk.get("standby") or not disk.get("smart"):
                        continue
                    identity = str(disk.get("serial") or disk.get("path") or disk.get("name"))
                    data = {"name": disk.get("name"), "model": disk.get("model"), "smart": disk["smart"]}
                    db.execute("INSERT INTO disks(ts,identity,data) VALUES (?,?,?)", (now, identity, json.dumps(data)))
            self.last_smart = now
        self.last_sample = now

    def history(self, query):
        hours = integer(query.get("hours", "24"), 1, 24 * 365)
        start = self.clock() - hours * 3600
        with self.store.db() as db:
            # Bucketing bounds response size even when a year's retention is selected.
            bucket = max(30, hours * 3600 // 720)
            rows = db.execute("SELECT ts,data FROM samples WHERE id IN (SELECT max(id) FROM samples WHERE ts>=? GROUP BY CAST(ts/? AS INTEGER)) ORDER BY ts", (start, bucket)).fetchall()
        return [{"ts": r["ts"], **json.loads(r["data"])} for r in rows]

    def disk_history(self, query):
        with self.store.db() as db:
            identities = [dict(r) for r in db.execute("SELECT identity,max(ts) AS last_seen FROM disks GROUP BY identity")]
            identity = query.get("identity") or (identities[0]["identity"] if identities else "")
            rows = db.execute("SELECT ts,data FROM disks WHERE identity=? ORDER BY ts DESC LIMIT 720", (identity,)).fetchall()
        points = [{"ts": row["ts"], **json.loads(row["data"])} for row in reversed(rows)]
        changes = []
        if len(points) > 1:
            first, last = points[0]["smart"], points[-1]["smart"]
            for key in sorted(set(first) | set(last)):
                if isinstance(first.get(key), (int, float)) and isinstance(last.get(key), (int, float)) and first[key] != last[key]:
                    changes.append({"metric": key, "before": first[key], "after": last[key], "delta": last[key] - first[key]})
        return {"devices": identities, "identity": identity, "points": points, "changes": changes}

    def notifications(self):
        config = self.store.get("notifications", {"enabled": False, "kind": "webhook"})
        public = {k: v for k, v in config.items() if k not in ("password", "secret")}
        public["has_secret"] = bool(config.get("secret"))
        public["has_password"] = bool(config.get("password"))
        with self.store.db() as db:
            deliveries = [dict(r) for r in db.execute("SELECT id,ts,status,attempts,error FROM deliveries ORDER BY id DESC LIMIT 100")]
        return {"config": public, "deliveries": deliveries}

    def save_notifications(self, body):
        if not isinstance(body.get("enabled", False), bool) or body.get("kind") not in ("webhook", "email"):
            raise Problem("通知配置无效")
        old = self.store.get("notifications", {})
        config = {"enabled": body.get("enabled", False), "kind": body["kind"]}
        if config["kind"] == "webhook":
            endpoint(body.get("url", ""))
            retained = old.get("secret", "") if body["url"] == old.get("url") else ""
            config.update(url=body["url"], secret=str(body.get("secret") or retained)[:512])
        else:
            for key in ("host", "username", "sender", "recipient"):
                value = str(body.get(key, "")).strip()
                if not value or len(value) > 254 or "\n" in value or "\r" in value:
                    raise Problem("邮件服务器、账号、发件人和收件人不能为空或包含换行")
                config[key] = value
            retained = old.get("password", "") if config["host"] == old.get("host") and config["username"] == old.get("username") else ""
            config.update(port=integer(body.get("port", 587), 1, 65535),
                          password=str(body.get("password") or retained)[:512])
        if not old.get("enabled") and config["enabled"]:
            self.store.set("notify_cursor", self.alert_snapshot().get("last_id", 0))
        self.store.set("notifications", config)
        self.notifications_enabled = config["enabled"]
        return self.notifications()

    def enqueue_alerts(self):
        config = self.store.get("notifications", {})
        if not config.get("enabled"):
            return
        snapshot = self.alert_snapshot()
        cursor = self.store.get("notify_cursor", 0)
        events = [event for event in snapshot.get("events", []) if event["id"] > cursor]
        if not events:
            return
        maintained = self.maintenance()["active"]
        with self.store.db() as db:
            for event in events:
                # Maintenance silences expected service events, never thermal/disk failures.
                suppressed = maintained and str(event.get("key", "")).startswith("svc:")
                db.execute("INSERT OR IGNORE INTO deliveries(event_id,ts,payload,status,next,error) VALUES (?,?,?,?,?,?)",
                           (event["id"], self.clock(), json.dumps(event), "suppressed" if suppressed else "pending", self.clock(), ""))
            db.execute("DELETE FROM deliveries WHERE id <= (SELECT max(id)-1000 FROM deliveries)")
        self.store.set("notify_cursor", max(cursor, snapshot.get("last_id", 0)))

    def send_notification(self, config, event):
        if config["kind"] == "webhook":
            headers = {"Authorization": "Bearer " + config["secret"]} if config.get("secret") else {}
            self.network(config["url"], headers, {"source": "fnOS-S2", "event": event})
        else:
            message = EmailMessage()
            message["From"], message["To"] = config["sender"], config["recipient"]
            message["Subject"] = "fnOS-S2 " + str(event.get("title", "设备告警")).replace("\n", " ")[:150]
            message.set_content(json.dumps(event, ensure_ascii=False, indent=2))
            with smtplib.SMTP(config["host"], config["port"], timeout=8) as smtp:
                check_address(smtp.sock.getpeername()[0])
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(config["username"], config["password"])
                smtp.send_message(message)

    def deliver_pending(self):
        config = self.store.get("notifications", {})
        if not config.get("enabled"):
            return
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM deliveries WHERE status='pending' AND next<=? ORDER BY id LIMIT 3", (self.clock(),)).fetchall()
        for row in rows:
            attempts = row["attempts"] + 1
            try:
                self.send_notification(config, json.loads(row["payload"]))
                status, error = "sent", ""
            except (OSError, ValueError, http.client.HTTPException, smtplib.SMTPException):
                status, error = ("failed" if attempts >= 4 else "pending"), "连接、认证或投递失败，请检查设置"
            with self.store.db() as db:
                db.execute("UPDATE deliveries SET status=?,attempts=?,next=?,error=? WHERE id=?", (status, attempts, self.clock() + min(3600, 30 * 2 ** attempts), error, row["id"]))

    def diagnostics(self):
        sensor = self.sensor()
        config = self.call("schedule_info") or {}
        cpu = number(sensor.get("cpu_temp"))
        checks = [{"id": "cpu", "title": "CPU 温度传感器", "ok": cpu is not None and 0 < cpu <= 125,
                   "detail": "读取系统温度传感器；无数据时请检查 thermal 驱动", "repair": ""},
                  {"id": "rtc", "title": "RTC 定时唤醒", "ok": bool(config.get("rtc_wake")),
                   "detail": "需主板支持 RTC 唤醒，并持续供电", "repair": ""}]
        for name, state in (sensor.get("services") or {}).items():
            checks.append({"id": name, "title": name, "ok": state in ("active", "done"),
                           "detail": "服务状态：" + str(state), "repair": name if state == "failed" else ""})
        checks.extend(self.call("diagnostics_files") or [])
        try:
            self.call("config_validate", {"config": self.configuration()})
            checks.append({"id": "config", "title": "硬件配置", "ok": True, "detail": "配置校验通过", "repair": ""})
        except Problem as error:
            checks.append({"id": "config", "title": "硬件配置", "ok": False, "detail": str(error), "repair": ""})
        return checks

    def report(self):
        sensor, info = self.sensor(), self.call("hwinfo") or {}
        return {"generated": self.clock(), "sensor": sensor, "hardware": info,
                "alerts": self.alert_snapshot(), "diagnostics": self.diagnostics(), "disk_trends": self.disk_history({})["changes"]}

    def save_device(self, body):
        parsed = endpoint(body.get("url", ""))
        if parsed.path not in ("", "/") or parsed.query:
            raise Problem("设备地址只包含协议、主机和端口")
        label = str(body.get("label", "")).strip()[:80]
        if not label:
            raise Problem("请填写设备名称")
        identifier = str(body.get("id") or secrets.token_hex(8))
        with self.store.db() as db:
            old = db.execute("SELECT token,url FROM devices WHERE id=?", (identifier,)).fetchone()
            if not old and db.execute("SELECT count(*) FROM devices").fetchone()[0] >= 20:
                raise Problem("最多登记 20 台设备")
            token = body.get("token") or (old["token"] if old and old["url"] == body["url"].rstrip("/") else "")
            if not isinstance(token, str) or not token.startswith("s2k_") or len(token) > 128:
                raise Problem("请提供目标设备签发的只读访问密钥")
            db.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?,?)", (identifier, label, body["url"].rstrip("/"), token))
        self.fleet_cache = None
        return {"id": identifier}

    def devices(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT id,label,url FROM devices ORDER BY label")]

    def fleet(self):
        if self.fleet_cache is not None and self.clock() - self.fleet_at < 15:
            return self.fleet_cache
        if not self.fleet_lock.acquire(blocking=False):
            if self.fleet_cache is not None:
                return self.fleet_cache
            raise Problem("正在检查设备，请稍后刷新", 503)
        try:
            self.fleet_cache = self._probe_fleet()
            self.fleet_at = self.clock()
            return self.fleet_cache
        finally:
            self.fleet_lock.release()

    def _probe_fleet(self):
        with self.store.db() as db:
            devices = [dict(r) for r in db.execute("SELECT * FROM devices LIMIT 20")]
        def probe(device):
            result = {k: v for k, v in device.items() if k != "token"}
            result["checked_at"] = self.clock()
            try:
                headers = {"Authorization": "Bearer " + device["token"]}
                state = self.network(device["url"] + "/api/sensor", headers)
                alerts = self.network(device["url"] + "/api/alerts", headers)
                if not state.get("ok") or not alerts.get("ok"):
                    raise Problem("目标设备拒绝请求")
                result.update(online=True, sensor=state.get("data"), alerts=alerts.get("data"))
            except (OSError, ValueError, http.client.HTTPException):
                result.update(online=False, error="无法连接或密钥失效")
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            return list(executor.map(probe, devices))

    def get(self, route, query, principal):
        if route == "me":
            return principal
        if route == "overview":
            return {"maintenance": self.maintenance(), "last_sample": self.last_sample,
                    "last_error": self.last_error, "retention_days": self.store.get("retention_days", 30),
                    "history_enabled": self.history_enabled}
        if route == "integration":
            return self.call("integration_status")
        if route == "history":
            return self.history(query)
        if route == "disks":
            return self.disk_history(query)
        if route == "diagnostics":
            return self.diagnostics()
        if route == "report":
            return self.report()
        if route == "schedule":
            return self.call("schedule_info")
        if route == "guard":
            return self.call("fan_guard_info")
        if route == "notifications":
            return self.notifications()
        if route == "keys":
            return self.store.keys()
        if route == "devices":
            return self.devices()
        if route == "fleet":
            return self.fleet()
        if route == "versions":
            with self.store.db() as db:
                return [dict(r) for r in db.execute("SELECT id,ts,actor,label FROM versions ORDER BY id DESC")]
        if route == "audit":
            with self.store.db() as db:
                return [dict(r) for r in db.execute("SELECT * FROM events WHERE action LIKE ? AND actor LIKE ? ORDER BY id DESC LIMIT 500", ("%" + query.get("action", "") + "%", "%" + query.get("actor", "") + "%"))]
        raise Problem("未知运维接口", 404)

    def post(self, route, body, principal):
        actor = principal["name"]
        if route == "backup":
            return self.backup(str(body.get("label", "手动备份")), actor)
        if route == "restore/preview":
            return self.restore_preview(body)
        if route == "restore":
            return self.restore(body, actor)
        if route == "retention":
            days = integer(body.get("days"), 1, 365)
            enabled = body.get("enabled", self.history_enabled)
            if not isinstance(enabled, bool):
                raise Problem("历史记录开关无效")
            with self.lock:
                self.store.set("retention_days", days)
                self.store.set("history_enabled", enabled)
                self.history_enabled = enabled
        elif route == "maintenance":
            return self.set_maintenance(body)
        elif route == "schedule":
            self.version("修改周计划前", actor)
            self.call("set_power_schedule", body)
            self.version("更新周计划", actor)
        elif route == "guard":
            self.version("修改保护策略前", actor)
            self.call("set_fan_guard", body)
            self.version("更新风扇保护", actor)
        elif route == "diagnostics/repair":
            allowed = {c["repair"] for c in self.diagnostics() if c["repair"]}
            if body.get("service") not in allowed:
                raise Problem("只能修复本次检查中失败的硬件服务")
            self.call("service", {"name": body["service"], "action": "restart"})
        elif route == "notifications":
            return self.save_notifications(body)
        elif route == "keys":
            return self.store.create_key(body)
        elif route == "keys/revoke":
            with self.store.db() as db:
                db.execute("UPDATE keys SET revoked=1 WHERE id=?", (body.get("id"),))
        elif route == "devices":
            return self.save_device(body)
        elif route == "devices/delete":
            with self.store.db() as db:
                db.execute("DELETE FROM devices WHERE id=?", (body.get("id"),))
            self.fleet_cache = None
        else:
            raise Problem("未知运维接口", 404)
        return {"saved": True}

    def start(self):
        if self.thread:
            return
        def loop():
            while not self.stop.is_set():
                try:
                    self.sample()
                    self.last_error = ""
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                    self.last_error = "采样或投递失败：" + type(error).__name__
                self.stop.wait(30)
        self.thread = threading.Thread(target=loop, name="s2-operations", daemon=True)
        self.thread.start()
