"""Standalone email/Webhook delivery, retaining existing notification settings."""
import contextlib, http.client, ipaddress, json, os, smtplib, socket, sqlite3, ssl, threading, time, urllib.parse
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from s2validation import Problem, integer

SEND_ERRORS = (OSError, ValueError, http.client.HTTPException, smtplib.SMTPException)
LEVEL_NAMES = {"crit": "严重", "warn": "警告", "ok": "已恢复", "info": "提示"}


def describe_error(error):
    """Turn a delivery exception into an actionable message without echoing credentials."""
    if isinstance(error, Problem):
        return str(error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "邮箱认证失败：账号或授权码错误。QQ / 163 / 126 等邮箱需在网页版开启 SMTP 服务，并填写授权码而不是登录密码"
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return "服务器不支持 STARTTLS，请把加密方式改为 SSL（端口 465）"
    if isinstance(error, smtplib.SMTPSenderRefused):
        return "发件地址被服务器拒绝：多数邮箱要求发件地址与登录账号一致"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "收件地址被服务器拒绝，请核对收件地址"
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return "服务器断开连接：端口与加密方式可能不匹配（465 用 SSL，587 用 STARTTLS）"
    if isinstance(error, smtplib.SMTPResponseException):
        return "邮件服务器拒绝投递：%s" % error.smtp_code
    if isinstance(error, ssl.SSLCertVerificationError):
        return "服务器证书校验失败，请核对服务器地址"
    if isinstance(error, ssl.SSLError):
        return "TLS 握手失败：端口与加密方式可能不匹配（465 用 SSL，587 用 STARTTLS）"
    if isinstance(error, socket.gaierror):
        return "无法解析服务器地址，请核对地址或设备的 DNS 设置"
    if isinstance(error, ConnectionRefusedError):
        return "连接被拒绝：端口可能错误"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "连接超时：请核对服务器地址和端口，并确认设备能访问外网"
    if isinstance(error, UnicodeError):
        return "账号或授权码包含服务器不支持的字符"
    return "投递失败（%s），请检查设置和设备网络" % type(error).__name__


def mail_security(config):
    security = config.get("security")
    if security in ("ssl", "starttls"):
        return security
    return "ssl" if config.get("port") == 465 else "starttls"


def event_text(event):
    level = LEVEL_NAMES.get(event.get("level"), "")
    lines = [str(event.get("title") or "设备告警")]
    if level:
        lines.append("级别：" + level)
    if event.get("ts"):
        lines.append("时间：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"])))
    if event.get("detail"):
        lines.append("详情：" + str(event["detail"]))
    lines.append("设备：" + socket.gethostname())
    return "\n".join(lines)

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

    def build_config(self, body, old):
        if not isinstance(body, dict) or not isinstance(body.get("enabled", False), bool) or body.get("kind") not in ("webhook", "email"):
            raise Problem("通知配置无效")
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
            security = body.get("security")
            if security not in (None, "", "ssl", "starttls"):
                raise Problem("加密方式无效")
            config["security"] = mail_security({"security": security, "port": config["port"]})
            if not config["password"]:
                raise Problem("请填写邮箱授权码")
        return config

    def save_notifications(self, body):
        old = self.store.get("notifications", {})
        config = self.build_config(body, old)
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
            level = LEVEL_NAMES.get(event.get("level"))
            title = str(event.get("title", "设备告警")).replace("\n", " ").replace("\r", " ")[:150]
            message["Subject"] = "fnOS-S2 " + ("【%s】" % level if level else "") + title
            message["Date"] = formatdate(localtime=True)
            message["Message-ID"] = make_msgid(domain=parseaddr(config["sender"])[1].rpartition("@")[2] or None)
            message.set_content(event_text(event))
            context = ssl.create_default_context()
            if mail_security(config) == "ssl":
                smtp = smtplib.SMTP_SSL(config["host"], config["port"], timeout=10, context=context)
            else:
                smtp = smtplib.SMTP(config["host"], config["port"], timeout=10)
            with smtp:
                check_address(smtp.sock.getpeername()[0])
                if mail_security(config) == "starttls":
                    smtp.starttls(context=context)
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
            except SEND_ERRORS as exc:
                status, error = ("failed" if attempts >= 4 else "pending"), describe_error(exc)
            with self.store.db() as db:
                db.execute("UPDATE deliveries SET status=?,attempts=?,next=?,error=? WHERE id=?", (status, attempts, self.clock() + min(3600, 30 * 2 ** attempts), error, row["id"]))


    def send_test(self, body=None):
        """Test the settings currently in the form when given, otherwise the saved ones."""
        stored = self.store.get("notifications", {})
        config = self.build_config(body, stored) if isinstance(body, dict) and body.get("kind") else stored
        if not config.get("kind"): raise Problem("请先填写通知设置")
        event={"title":"测试通知", "level":"info", "ts":int(self.clock()), "detail":"fnOS-S2 告警通知连接测试，收到即表示设置可用"}
        try:
            self.send_notification(config,event); status,error="sent",""
        except SEND_ERRORS as exc:
            status,error="failed","测试投递失败：" + describe_error(exc)
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
