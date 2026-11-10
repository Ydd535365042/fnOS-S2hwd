"""Source-bound regression tests; all hardware/process boundaries are stubbed.

Run: python -I -B tests/test_security.py
"""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

SOURCE = Path(__file__).resolve().parents[1] / "app"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def serving(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


class Sandbox:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="s2-regression-")
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            "S2_VAR_DIR": str(self.root / "var"), "S2_ETC_DIR": str(self.root / "etc"),
            "S2_APP_CONF_DIR": str(self.root / "conf"), "S2_PATCH_DIR": str(self.root / "patches"),
            "S2_AGENT_KEY_FILE": str(self.root / "agent.key"),
            "S2_HWCTL_DIR": str(SOURCE / "server"), "TOKEN": "fixture-password",
            "S2_POWER_DRYRUN": "1", "S2_ALERTS": "0", "TRIM_DATA_SHARE_PATHS": "",
            "S2_AGENT_URL": "http://127.0.0.1:1",
        })
        self.env.start()
        self.web = load("s2_test_web", SOURCE / "server/server.py")
        self.hw = self.web.hwctl
        self.calls, self.jobs = [], []
        self.patches = [
            mock.patch.object(self.hw, "SCRIPTS_DIR", self.web.SCRIPTS_DIR),
            mock.patch.object(self.hw, "SHARE_SCRIPTS", ""),
            mock.patch.object(self.hw, "is_root", return_value=True),
            mock.patch.object(self.hw, "dispatch", side_effect=self.dispatch),
            mock.patch.object(self.hw, "run", side_effect=AssertionError("unexpected hardware process")),
            mock.patch.object(self.hw, "collect_sensor", return_value={}),
            mock.patch.object(self.web, "start_job", side_effect=lambda p, **kw: self.jobs.append(p) or "a" * 32),
        ]
        for patch in self.patches:
            patch.start()
        class Handler(self.web.Handler):
            def _remote(self):
                return "192.0.2.10"

            def log_request(self, *args):
                pass

        self.handler = Handler

    def dispatch(self, op, params):
        self.calls.append((op, params))
        return True, {"stub": True}

    def close(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.env.stop()
        self.temp.cleanup()


def request(server, path, body=None, headers=None, method="POST"):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        raw = json.dumps(body) if isinstance(body, (dict, list)) else body
        connection.request(method, path, raw, headers or {})
        response = connection.getresponse()
        content = response.read()
        data = json.loads(content) if "application/json" in response.getheader("Content-Type", "") else content
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Sandbox()
        cls.web = cls.box.web
        cls.context = serving(cls.box.handler)
        cls.server = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        cls.box.close()

    def setUp(self):
        self.web.TOKEN = "fixture-password"
        self.web.AUTH_FAILS.clear()
        self.web.SESSIONS = self.web.s2security.Sessions()
        self.box.calls.clear()
        self.box.jobs.clear()

    def login(self):
        status, headers, data = request(self.server, "/login", "token=fixture-password", {
            "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotEqual(data["session"], self.web.TOKEN)
        return data["session"]

    def headers(self):
        return {"Authorization": "Bearer " + self.login(), "X-S2-Request": "1",
                "Content-Type": "application/json"}

    def test_cookie_and_query_are_not_credentials(self):
        for headers, query in [({"Cookie": "s2token=fixture-password"}, ""), ({}, "?token=fixture-password")]:
            status, response_headers, _ = request(self.server, "/api/state" + query, headers=headers, method="GET")
            self.assertEqual(status, 401)
            if headers:
                self.assertIn("Max-Age=0", response_headers["Set-Cookie"])
        self.assertEqual(request(self.server, "/?token=fixture-password", method="GET")[0], 303)

    def test_session_auth_and_logout(self):
        headers = self.headers()
        self.assertEqual(request(self.server, "/api/state", headers=headers, method="GET")[0], 200)
        self.assertEqual(request(self.server, "/api/logout", {}, headers)[0], 200)
        self.assertEqual(request(self.server, "/api/state", headers=headers, method="GET")[0], 401)

    def test_cross_origin_save_start_upload_and_login_rejected(self):
        headers = self.headers()
        headers.update({"Origin": "http://127.0.0.1:19999", "Sec-Fetch-Site": "same-site"})
        for path in ("/api/script/save", "/api/script/start", "/api/script/upload?name=blocked.sh", "/api/hw", "/login"):
            with self.subTest(path=path):
                self.assertEqual(request(self.server, path, {"name": "blocked.sh", "content": "echo blocked"}, headers)[0], 403)
        self.assertFalse(self.box.jobs)
        self.assertFalse(self.box.calls)
        self.assertFalse((self.box.root / "var/scripts/blocked.sh").exists())

    def test_simple_post_and_missing_marker_rejected(self):
        headers = self.headers()
        headers["Content-Type"] = "text/plain"
        self.assertEqual(request(self.server, "/api/script/save", {}, headers)[0], 415)
        del headers["X-S2-Request"]
        self.assertEqual(request(self.server, "/api/script/save", {}, headers)[0], 403)

    def test_authenticated_mutations_and_upload_still_work(self):
        headers = self.headers()
        headers.update({"Origin": "http://127.0.0.1:" + str(self.server.server_port), "Sec-Fetch-Site": "same-origin"})
        for op in ("set_power_schedule", "service", "set_fan", "set_port"):
            self.assertTrue(request(self.server, "/api/hw", {"op": op, "params": {}}, headers)[2]["ok"])
        self.assertEqual(len([c for c in self.box.calls if c[0] != "config_snapshot"]), 4)
        self.assertTrue(request(self.server, "/api/script/save", {"name": "allowed.sh", "content": "echo allowed\n"}, headers)[2]["ok"])
        inspected = request(self.server, "/api/script/inspect?name=allowed.sh", headers=headers, method="GET")[2]["data"]
        self.assertTrue(request(self.server, "/api/script/start", {"name": "allowed.sh", "sha256": inspected["sha256"]}, headers)[2]["ok"])
        self.assertEqual(len(self.box.jobs), 1)
        headers["Content-Type"] = "application/octet-stream"
        self.assertTrue(request(self.server, "/api/script/upload?name=upload.sh", b"#!/bin/sh\necho fixture\n", headers)[2]["ok"])

    def test_no_password_remote_write_operations_denied(self):
        self.web.TOKEN = ""
        headers = {"Content-Type": "application/json", "X-S2-Request": "1"}
        for prefix in ("/api/", "/api/v1/"):
            for op in ("set_power_schedule", "service", "set_fan", "fan_test", "set_led_override", "beep_test", "set_beep", "set_port", "patch", "future_operation"):
                with self.subTest(op=op, prefix=prefix):
                    self.assertEqual(request(self.server, prefix + "hw", {"op": op, "params": {}}, headers)[0], 403)
            for path in ("script/save", "script/start", "script/upload", "restart", "power/prepare"):
                self.assertEqual(request(self.server, prefix + path, {}, headers)[0], 403)
        self.assertFalse(self.box.calls)
        self.assertFalse(self.box.jobs)
        self.assertEqual(request(self.server, "/api/hw", {"op": "status"}, headers)[0], 200)

    def test_no_password_loopback_native_write_allowed(self):
        self.web.TOKEN = ""
        with mock.patch.object(self.box.handler, "_remote", return_value="127.0.0.1"):
            self.assertEqual(request(self.server, "/api/hw", {"op": "set_fan"}, {
                "Content-Type": "application/json", "X-S2-Request": "1"})[0], 200)

    def test_parameter_types_and_power_bypass_rejected(self):
        headers = self.headers()
        for body in ([], {"op": []}, {"op": "set_fan", "params": []}, {"op": "set_fan", "params": None}, {"op": "power"}):
            self.assertEqual(request(self.server, "/api/hw", body, headers)[0], 400)
        self.assertFalse(self.box.calls)

    def test_expiration_eviction_and_hashed_storage(self):
        sessions = self.web.s2security.Sessions(ttl=5, limit=2)
        with mock.patch.object(self.web.s2security.time, "monotonic", return_value=100):
            first = sessions.issue()
            self.assertTrue(sessions.valid(first))
            self.assertNotIn(first, sessions.entries)
            sessions.issue()
            last = sessions.issue()
            self.assertEqual(len(sessions.entries), 2)
            self.assertFalse(sessions.valid(first))
        with mock.patch.object(self.web.s2security.time, "monotonic", return_value=106):
            self.assertFalse(sessions.valid(last))

    def test_settings_atomic_write_preserves_content_and_rejects_symlink(self):
        settings = self.box.root / "settings.conf"
        settings.write_text("TOKEN='fixture-password'\nPORT='18899'\n", encoding="utf-8")
        self.box.hw.write_kv(str(settings), {"PORT": "19999"})
        text = settings.read_text(encoding="utf-8")
        self.assertIn("fixture-password", text)
        self.assertIn("19999", text)
        self.assertFalse(list(self.box.root.glob(".s2-*")))
        link = self.box.root / "settings-link.conf"
        try:
            link.symlink_to(settings)
        except OSError:
            return  # Windows may not grant symlink creation.
        self.assertRaises(ValueError, self.web.s2security.atomic_text, str(link), "changed")

    @unittest.skipUnless(os.name == "posix", "requires Linux/POSIX permission semantics")
    def test_settings_mode_under_umask_022(self):
        settings = self.box.root / "settings.conf"
        settings.write_text("TOKEN='fixture-password'\n")
        os.chmod(settings, 0o600)
        mask = os.umask(0o022)
        try:
            self.box.hw.write_kv(str(settings), {"PORT": "19999"})
        finally:
            os.umask(mask)
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)

    def test_agent_requires_key_and_valid_protocol(self):
        agent = load("s2_test_agent", SOURCE / "agent/s2hw-agent.py")
        key = "ab" * 32
        with mock.patch.object(agent.s2security, "read_agent_key", return_value=key), serving(agent.H) as server:
            for method, path in (("GET", "/ping"), ("POST", "/hw")):
                self.assertEqual(request(server, path, method=method)[0], 401)
                self.assertEqual(request(server, path, headers={"X-S2-Agent-Key": "wrong"}, method=method)[0], 401)
            headers = {"X-S2-Agent-Key": key, "Content-Type": "application/json"}
            self.assertEqual(request(server, "/ping", headers=headers, method="GET")[0], 200)
            self.assertEqual(request(server, "/hw", {"op": "set_fan"}, headers)[0], 200)
            self.assertEqual(request(server, "/not-hw", {"op": "power"}, headers)[0], 404)
            self.assertEqual(request(server, "/hw", {"op": "set_fan", "params": []}, headers)[0], 400)
            headers["Origin"] = "http://127.0.0.1:1234"
            self.assertEqual(request(server, "/hw", {"op": "power"}, headers)[0], 400)
        self.assertEqual(self.box.calls, [("set_fan", {})])

    def test_agent_missing_key_fails_closed(self):
        self.assertEqual(self.web.s2security.read_agent_key(), "")
        self.assertFalse(self.web.agent_call("set_fan", {})["ok"])

    def test_login_rate_limit_still_applies(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        for _ in range(self.web.FAIL_MAX):
            self.assertEqual(request(self.server, "/login", "token=wrong", headers)[0], 401)
        self.assertEqual(request(self.server, "/login", "token=fixture-password", headers)[0], 429)

    def test_agent_authenticated_probe_and_forwarding(self):
        agent = load("s2_forward_agent", SOURCE / "agent/s2hw-agent.py")
        with mock.patch.object(agent.s2security, "read_agent_key", return_value="ab" * 32), serving(agent.H) as server:
            with mock.patch.object(self.web, "AGENT", "http://127.0.0.1:" + str(server.server_port)):
                self.web._AGENT_STATE.update(ok=None, ts=0)
                self.assertTrue(self.web.agent_available())
                self.assertTrue(self.web.agent_call("set_beep", {"ms": 50})["ok"])
        self.assertEqual(self.box.calls, [("set_beep", {"ms": 50})])

    def test_agent_key_file_format(self):
        path = self.box.root / "agent.key"
        try:
            path.write_text("ab" * 32 + "\n", encoding="ascii")
            os.chmod(path, 0o640)
            expected = "ab" * 32 if os.name != "posix" or os.geteuid() == 0 else ""
            self.assertEqual(self.web.s2security.read_agent_key(), expected)
            path.write_text("invalid-key", encoding="ascii")
            self.assertEqual(self.web.s2security.read_agent_key(), "")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
