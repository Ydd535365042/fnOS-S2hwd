"""Standalone email/Webhook delivery, retaining existing notification settings."""
import contextlib, http.client, ipaddress, json, os, smtplib, socket, sqlite3, ssl, threading, time, urllib.parse
from email.message import EmailMessage
from s2validation import Problem, integer

class NotificationStore:
    def __init__(self, directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, "operations.sqlite3")  # Existing credentials stay in place.
        self.lock = threading.RLock()
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE, ts REAL, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0, next REAL, error TEXT);")
    @contextlib.contextmanager
    def db(self):
        with self.lock:
            db=sqlite3.connect(self.path, timeout=10); db.row_factory=sqlite3.Row
            try:
                with db: yield db
            finally: db.close()
    def get(self,key,default=None):
        with self.db() as db: row=db.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,key,value):
        with self.db() as db: db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",(key,json.dumps(value)))

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


class Notifier:
    def __init__(self, directory, alert_snapshot, clock=time.time, network=network_json):
        self.store=NotificationStore(directory)
        self.alert_snapshot,self.clock,self.network=alert_snapshot,clock,network
        self.delivery_lock=threading.Lock()
        self.notifications_enabled=bool(self.store.get("notifications",{}).get("enabled"))
        self.stop=threading.Event(); self.thread=None; self.last_error=""

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
                raw = body.get(key, "")
                if not isinstance(raw, str) or "\n" in raw or "\r" in raw:
                    raise Problem("邮件服务器、账号、发件人和收件人不能为空或包含换行")
                value = raw.strip()
                if not value or len(value) > 254:
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
        with self.store.db() as db:
            for event in events:
                # Maintenance silences expected service events, never thermal/disk failures.
                db.execute("INSERT OR IGNORE INTO deliveries(event_id,ts,payload,status,next,error) VALUES (?,?,?,?,?,?)",
                           (event["id"], self.clock(), json.dumps(event), "pending", self.clock(), ""))
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
        if not self.delivery_lock.acquire(blocking=False):
            return
        try:
            self._deliver_pending()
        finally:
            self.delivery_lock.release()

    def _deliver_pending(self):
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


    def send_test(self):
        config=self.store.get("notifications",{})
        if not config.get("kind"): raise Problem("请先保存通知设置")
        event={"title":"测试通知", "detail":"fnOS-S2 告警通知连接测试"}
        try:
            self.send_notification(config,event); status,error="sent",""
        except (OSError, ValueError, http.client.HTTPException, smtplib.SMTPException):
            status,error="failed","测试投递失败，请核对服务器、凭据及设备网络"
        with self.store.db() as db:
            db.execute("INSERT INTO deliveries(event_id,ts,payload,status,attempts,next,error) VALUES (NULL,?,?,?,?,?,?)",(self.clock(),json.dumps(event),status,1,self.clock(),error))
        if error: raise Problem(error,502)
        return {"sent":True}

    def start(self):
        if self.thread: return
        def loop():
            while not self.stop.is_set():
                if self.notifications_enabled:
                    try: self.enqueue_alerts(); self.deliver_pending(); self.last_error=""
                    except (OSError,ValueError,RuntimeError,sqlite3.Error): self.last_error="通知处理失败，请检查设置和日志"
                self.stop.wait(5)
        self.thread=threading.Thread(target=loop,name="s2-notifications",daemon=True); self.thread.start()
