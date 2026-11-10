"""Small, shared security primitives; Python standard library only."""
import hashlib
import os
import secrets
import stat
import tempfile
import threading
import time


class Sessions:
    def __init__(self, ttl=12 * 3600, limit=128):
        self.ttl, self.limit = ttl, limit
        self.entries = {}
        self.subjects = {}
        self.lock = threading.Lock()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, subject=None):
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if v > now}
            if len(self.entries) >= self.limit:
                del self.entries[min(self.entries, key=self.entries.get)]
            self.subjects = {k: v for k, v in self.subjects.items() if k in self.entries}
            digest = self.digest(token)
            self.entries[digest] = now + self.ttl
            self.subjects[digest] = subject
        return token

    def subject(self, token):
        with self.lock:
            return self.subjects.get(self.digest(token))

    def valid(self, token):
        if not token or len(token) > 128:
            return False
        with self.lock:
            return self.entries.get(self.digest(token), 0) > time.monotonic()

    def revoke(self, token):
        with self.lock:
            self.entries.pop(self.digest(token), None)
            self.subjects.pop(self.digest(token), None)


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
