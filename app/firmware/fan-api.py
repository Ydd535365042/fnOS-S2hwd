#!/usr/bin/env python3
# =============================================================
# fan-api.py —— 风扇控制的 HTTP 接口（为后期页面手动调节预留）
#
# 设计要点
#   1) 只做薄薄一层：把 HTTP 请求翻译成对
#        /var/lib/fan-control/mode
#        /var/lib/fan-control/level
#      两个文件的读写，真正的调速逻辑仍在 fan-control.sh 里。
#      这样即使本接口不启动，页面后端直接写那两个文件也一样能用。
#   2) 默认【只监听 127.0.0.1】，且 service 默认不 enable，
#      避免 NAS 上多开一个无鉴权的端口。需要局域网直连时改
#      /etc/fan-api.conf 的 BIND。
#   3) 可选 TOKEN 鉴权：conf 里配了 TOKEN 后，请求需带 ?token= 或
#      X-Token 头。
#
# 接口
#   GET  /status                      查询状态（JSON）
#   GET  /set?mode=manual&level=8     设置（GET 形式，方便页面直接拼 URL）
#   POST /set   {"mode":"manual","level":8}
#   mode 取值：auto | manual ；level 取值：0-15（0=停转，15=全速）
#
# 启停
#   systemctl enable --now fan-api    # 需要时才开
#   systemctl stop fan-api
# =============================================================

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

RUN_DIR = "/var/lib/fan-control"
MODE_FILE = os.path.join(RUN_DIR, "mode")
LEVEL_FILE = os.path.join(RUN_DIR, "level")
STATUS_FILE = os.path.join(RUN_DIR, "status")
API_CONF = "/etc/fan-api.conf"

BIND = "127.0.0.1"
PORT = 8899
TOKEN = ""


def load_conf():
    """读取 /etc/fan-api.conf（KEY=VALUE 形式），缺失则用默认值。"""
    global BIND, PORT, TOKEN
    try:
        with open(API_CONF, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "BIND":
                    BIND = v
                elif k == "PORT":
                    try:
                        PORT = int(v)
                    except ValueError:
                        pass
                elif k == "TOKEN":
                    TOKEN = v
    except FileNotFoundError:
        pass


def read_kv(path):
    """把 key=value 状态文件读成 dict。"""
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k] = v
    except FileNotFoundError:
        pass
    return data


def current_level():
    try:
        with open(LEVEL_FILE, "r", encoding="utf-8") as fh:
            v = int(fh.read().strip())
            return v
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["bash", "-c",
             "grep -E '^MANUAL_LEVEL=' /etc/fan-control.conf 2>/dev/null | tail -1 | cut -d= -f2"],
            capture_output=True, text=True, timeout=3)
        return int(out.stdout.strip())
    except Exception:
        return 6


def get_status():
    st = read_kv(STATUS_FILE)
    mode = st.get("mode", "auto")
    try:
        level = int(st.get("level", "0"))
    except ValueError:
        level = 0
    disks_raw = st.get("disks", "")
    disks = {}
    for item in disks_raw.split():
        if "=" in item:
            k, v = item.split("=", 1)
            try:
                disks[k] = int(v)
            except ValueError:
                disks[k] = v
    return {
        "ok": True,
        "mode": mode,
        "level": level,
        "manual_level": current_level(),
        "duty_pct": int(st.get("duty_pct", "0") or 0),
        "cpu_temp": int(st.get("cpu_temp", "0") or 0),
        "hdd_max_temp": int(st.get("hdd_max_temp", "0") or 0),
        "hdd_source": st.get("hdd_source", "none"),
        "disks": disks,
        "timestamp": st.get("timestamp", ""),
        "service_running": bool(st),
    }


def apply_set(mode=None, level=None):
    """已废弃。

    fan-control.sh v5 改成「四锚点百分比曲线 + 三档预设」之后，运行态文件
    /var/lib/fan-control/{mode,level} 不再被读取 —— 挡位这个概念整个没有了。
    这个接口保留只是为了给老调用方一个明确的答复，而不是让它「写成功但没效果」。
    要改风扇行为请走控制台的 set_fan（tempProfile + hdd/cpu 锚点）。
    """
    return False, ("已废弃：风扇改用三档曲线（quiet/performance/custom），"
                   "不再支持 mode/level。请使用控制台的风扇面板或 /api/hw set_fan。")


class Handler(BaseHTTPRequestHandler):
    server_version = "fan-api/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self, query):
        if not TOKEN:
            return True
        if self.headers.get("X-Token") == TOKEN:
            return True
        got = query.get("token", [""])[0]
        return got == TOKEN

    def log_message(self, fmt, *args):
        sys.stderr.write("[fan-api] %s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if not self._auth_ok(q):
            return self._send(401, {"ok": False, "error": "unauthorized"})

        if path in ("/", ""):
            return self._send(200, {
                "ok": True,
                "usage": {
                    "GET /status": "查询状态",
                    "GET /set?mode=manual&level=8": "设置模式与挡位",
                    "POST /set": 'body: {"mode":"manual","level":8}',
                },
                "range": {"mode": ["auto", "manual"], "level": "0-15"},
            })

        if path == "/status":
            return self._send(200, get_status())

        if path == "/set":
            ok, msg = apply_set(q.get("mode", [None])[0], q.get("level", [None])[0])
            if not ok:
                return self._send(400, {"ok": False, "error": msg})
            return self._send(200, {"ok": True, "result": msg, "status": get_status()})

        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if not self._auth_ok(q):
            return self._send(401, {"ok": False, "error": "unauthorized"})

        if path not in ("/set", "/status"):
            return self._send(404, {"ok": False, "error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        mode = level = None
        if raw:
            try:
                payload = json.loads(raw)
                mode = payload.get("mode")
                level = payload.get("level")
            except (ValueError, AttributeError):
                pass
        # query 参数优先级更高，方便 GET 风格调试
        if "mode" in q:
            mode = q["mode"][0]
        if "level" in q:
            level = q["level"][0]

        if path == "/status":
            return self._send(200, get_status())

        ok, msg = apply_set(mode, level)
        if not ok:
            return self._send(400, {"ok": False, "error": msg})
        return self._send(200, {"ok": True, "result": msg, "status": get_status()})


def main():
    load_conf()
    os.makedirs(RUN_DIR, exist_ok=True)
    srv = HTTPServer((BIND, PORT), Handler)
    sys.stderr.write("[fan-api] listening on %s:%d\n" % (BIND, PORT))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
