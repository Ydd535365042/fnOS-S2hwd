"""Small, shared security primitives; Python standard library only."""
import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
import threading
import time


class Sessions:
    def __init__(self, ttl=None, limit=128, path=None, binding=""):
        self.ttl, self.limit = ttl, limit
        self.path = path
        self.binding = self.digest(binding)
        self.entries = {}
        self.subjects = {}
        self.lock = threading.Lock()
        if path:
            self._load()

    def _load(self):
        try:
            if not stat.S_ISREG(os.lstat(self.path).st_mode):
                raise ValueError("session storage must be a regular file")
            with open(self.path, encoding="utf-8") as stream:
                data = json.loads(stream.read(2 * 1024 * 1024))
            if not isinstance(data, dict) or data.get("version") != 1:
                return
            if data.get("binding") != self.binding:
                self._save()  # Changing the access passphrase invalidates old sessions permanently.
                return
            entries = data.get("entries", {})
            if not isinstance(entries, dict):
                return
            for digest, entry in list(entries.items())[-self.limit:]:
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    continue
                if not isinstance(entry, dict):
                    continue
                expires, subject = entry.get("expires"), entry.get("subject")
                if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()):
                    continue
                if subject is not None and not isinstance(subject, dict):
                    continue
                self.entries[digest], self.subjects[digest] = expires, subject
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, UnicodeError):
            pass  # Corrupt data never grants a session.

    def _save(self):
        if self.path:
            data = {"version": 1, "binding": self.binding, "entries": {
                key: {"expires": expires, "subject": self.subjects.get(key)} for key, expires in self.entries.items()}}
            atomic_text(self.path, json.dumps(data, ensure_ascii=False), mode=0o600)

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, subject=None):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            previous = self.entries.copy(), self.subjects.copy()
            self.entries = {k: v for k, v in self.entries.items() if v is None or v > now}
            if len(self.entries) >= self.limit:
                del self.entries[next(iter(self.entries))]
            self.subjects = {k: v for k, v in self.subjects.items() if k in self.entries}
            digest = self.digest(token)
            self.entries[digest] = None if self.ttl is None else now + self.ttl
            self.subjects[digest] = subject
            try:
                self._save()
            except (OSError, ValueError):
                self.entries, self.subjects = previous
                raise
        return token

    def subject(self, token):
        with self.lock:
            return self.subjects.get(self.digest(token))

    def valid(self, token):
        if not token or len(token) > 128:
            return False
        with self.lock:
            digest = self.digest(token)
            return digest in self.entries and (self.entries[digest] is None or self.entries[digest] > time.time())

    def revoke(self, token):
        with self.lock:
            digest = self.digest(token)
            if digest not in self.entries:
                return
            previous = self.entries.copy(), self.subjects.copy()
            self.entries.pop(digest, None)
            self.subjects.pop(digest, None)
            try:
                self._save()
            except (OSError, ValueError):
                self.entries, self.subjects = previous
                raise


def atomic_text(path, text, mode=None):
    """Replace a regular file without exposing its contents through a loose tmp mode."""
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    previous = None
    try:
        previous = os.lstat(path)
    except FileNotFoundError:
        pass
    if previous and not stat.S_ISREG(previous.st_mode):
        raise ValueError("refusing to replace a non-regular file")
    target_mode = mode if mode is not None else (stat.S_IMODE(previous.st_mode) if previous else 0o600)
    fd, tmp = tempfile.mkstemp(prefix=".s2-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            if os.name == "posix":
                if previous and os.geteuid() == 0:
                    os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                os.fchmod(stream.fileno(), target_mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def agent_key_path():
    return os.environ.get("S2_AGENT_KEY_FILE", os.path.join(
        os.environ.get("S2_ETC_DIR", "/var/apps/fnOS-S2/etc"), "agent.key"))


def read_agent_key():
    """Fail closed if absent or exposed to unrelated local users."""
    try:
        path = agent_key_path()
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return ""
        if os.name == "posix" and (st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o027):
            return ""  # Only root and an explicitly assigned read-only group.
        with open(path, encoding="ascii") as stream:
            key = stream.read(129).strip()
        return key if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else ""
    except (OSError, UnicodeError):
        return ""


def init_agent_key():
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("agent key initialization requires root on Linux")
    import grp
    group = os.environ.get("S2_AGENT_GROUP", "fnOS-S2")
    gid = grp.getgrnam(group).gr_gid
    path = agent_key_path()
    os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    if not read_agent_key():
        raise RuntimeError("existing agent key has unsafe ownership, permissions or format")
    os.chown(path, 0, gid)
    os.chmod(path, 0o640)
