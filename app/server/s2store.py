"""Private SQLite storage and scoped API keys for the operations console."""
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

ALL_SCOPES = ["read", "control", "scripts", "admin"]


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

    def create_key(self, body):
        scopes = body.get("scopes", ["read"])
        if not isinstance(scopes, list) or not scopes or any(s not in ALL_SCOPES for s in scopes):
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
