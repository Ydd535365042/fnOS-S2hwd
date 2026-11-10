"""Private SQLite storage and account authorization for the operations console."""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time

ROLES = {"viewer": ["read"], "operator": ["read", "control"],
         "admin": ["read", "control", "scripts", "admin"]}


class Problem(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def integer(value, low, high):
    if isinstance(value, bool):
        raise Problem("请输入有效整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise Problem("请输入有效整数") from None
    if str(number) != str(value) or not low <= number <= high:
        raise Problem("数值超出允许范围")
    return number


class Store:
    def __init__(self, directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, "operations.sqlite3")
        self.lock = threading.RLock()
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt TEXT, password TEXT,
                    role TEXT, enabled INTEGER, revision INTEGER);
                CREATE TABLE IF NOT EXISTS keys(id TEXT PRIMARY KEY, label TEXT, hash TEXT UNIQUE,
                    scopes TEXT, expires REAL, created REAL, last_used REAL, revoked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, actor TEXT, ip TEXT,
                    action TEXT, ok INTEGER, detail TEXT);
                CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, ts REAL, actor TEXT,
                    label TEXT, config TEXT);
                CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, ts REAL, data TEXT);
                CREATE INDEX IF NOT EXISTS sample_time ON samples(ts);
                CREATE TABLE IF NOT EXISTS disks(id INTEGER PRIMARY KEY, ts REAL, identity TEXT, data TEXT);
                CREATE INDEX IF NOT EXISTS disk_time ON disks(identity,ts);
                CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, event_id INTEGER UNIQUE,
                    ts REAL, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0, next REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, label TEXT, url TEXT, token TEXT);
                CREATE TABLE IF NOT EXISTS scripts(name TEXT PRIMARY KEY, hash TEXT, content TEXT);
            ''')

    @contextlib.contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def audit(self, action, actor, ip, ok, detail=None):
        # Caller supplies only identifiers and operation results, never request bodies/secrets.
        with self.db() as db:
            db.execute("INSERT INTO events(ts,actor,ip,action,ok,detail) VALUES (?,?,?,?,?,?)",
                       (time.time(), actor, ip, action, int(bool(ok)), json.dumps(detail or {}, ensure_ascii=False)))
            db.execute("DELETE FROM events WHERE id <= (SELECT max(id)-10000 FROM events)")

    def users_exist(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM users WHERE enabled=1 LIMIT 1").fetchone())

    def users(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT name,role,enabled,revision FROM users ORDER BY name")]

    @staticmethod
    def password_hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()

    def save_user(self, body):
        name, role = body.get("name", ""), body.get("role", "viewer")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", name) or name == "owner":
            raise Problem("账号需为 2–32 位字母、数字、点、短横线或下划线；owner 为保留名称")
        if role not in ROLES or not isinstance(body.get("enabled", True), bool):
            raise Problem("账号角色或启用状态无效")
        password = body.get("password", "")
        if not isinstance(password, str) or (password and not 12 <= len(password) <= 128):
            raise Problem("新密码需要 12–128 位")
        with self.db() as db:
            old = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
            if not db.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1").fetchone() and (role != "admin" or not body.get("enabled", True)):
                raise Problem("请先创建一个启用的管理员账号")
            if not old and not password:
                raise Problem("新增账号必须设置密码")
            if old and old["role"] == "admin" and old["enabled"] and (role != "admin" or not body.get("enabled", True)):
                count = db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
                if count <= 1:
                    raise Problem("请保留至少一个启用的管理员账号")
            salt = secrets.token_hex(16) if password else old["salt"]
            digest = self.password_hash(password, salt) if password else old["password"]
            db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?)", (
                name, salt, digest, role, int(body.get("enabled", True)), old["revision"] + 1 if old else 1))

    def login(self, name, password):
        if not isinstance(password, str) or len(password) > 128:
            return None
        with self.db() as db:
            row = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        salt = row["salt"] if row else "00" * 16
        candidate = self.password_hash(password, salt)
        if row and row["enabled"] and hmac.compare_digest(candidate, row["password"]):
            return {"name": name, "role": row["role"], "revision": row["revision"], "kind": "user"}
        return None

    def current_user(self, subject):
        with self.db() as db:
            row = db.execute("SELECT role,revision,enabled FROM users WHERE name=?", (subject.get("name"),)).fetchone()
        if row and row["enabled"] and row["revision"] == subject.get("revision"):
            return dict(subject, role=row["role"], scopes=ROLES[row["role"]])
        return None

    def create_key(self, body):
        scopes = body.get("scopes", ["read"])
        if not isinstance(scopes, list) or not scopes or any(s not in ROLES["admin"] for s in scopes):
            raise Problem("密钥权限无效")
        label = str(body.get("label", ""))[:80].strip()
        if not label:
            raise Problem("请填写密钥用途")
        token = "s2k_" + secrets.token_urlsafe(32)
        identifier, now = secrets.token_hex(8), time.time()
        expires = now + integer(body.get("days", 30), 1, 365) * 86400
        with self.db() as db:
            db.execute("INSERT INTO keys(id,label,hash,scopes,expires,created,last_used) VALUES (?,?,?,?,?,?,?)",
                       (identifier, label, hashlib.sha256(token.encode()).hexdigest(), json.dumps(scopes), expires, now, 0))
        return {"id": identifier, "token": token, "expires": expires}

    def key_principal(self, token):
        if len(token) > 128:
            return None
        with self.db() as db:
            row = db.execute("SELECT * FROM keys WHERE hash=? AND revoked=0 AND expires>?",
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if row:
                db.execute("UPDATE keys SET last_used=? WHERE id=?", (time.time(), row["id"]))
                return {"name": "key:" + row["label"], "kind": "key", "scopes": json.loads(row["scopes"])}
        return None

    def keys(self):
        with self.db() as db:
            result = [dict(r) for r in db.execute("SELECT id,label,scopes,expires,created,last_used,revoked FROM keys ORDER BY created DESC")]
        for row in result:
            row["scopes"] = json.loads(row["scopes"])
        return result
