#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S2 硬件控制台 —— HTTP 服务（仅用 Python 标准库）

路由（同时兼容 /api/xxx 与 /api/v1/xxx 两种前缀）
----
GET  /                     静态页面（命中时带 ETag/Last-Modified，304 省流量）；
                           公开静态应用壳，所有设备数据均经 API 鉴权
POST /login                表单 token / next，返回持久会话（不设置认证 Cookie）
GET  /api/state            综合状态（附 access: {token 是否设置, danger 本次来源能否做高危操作}）
GET  /api/sensor           轻量传感器快照（10 秒轮询，不起子进程）
GET  /api/hwinfo?force=    慢变硬件信息（CPU/内存/磁盘/SMART/文件系统/阵列/网卡），
                           后端自带 60s/600s 缓存；非 root 走 agent 代采 SMART
POST /api/hw               {op, params}  硬件操作（root 直控 / agent 代理）
POST /api/power/prepare    {action}  取一次性确认码（目前只有 reboot）
POST /api/power            {action, nonce}  立即重启
POST /api/script/save      {name, content}
POST /api/script/start     {name}
GET  /api/script/poll?job= 任务输出
GET  /api/log?name=        日志尾部
POST /api/restart          {port} 改端口并自重启

安全设计
--------
1) 访问口令使用 hmac.compare_digest 做常量时间比较，避免时序侧信道。
2) 静态文件先 realpath 归一化，再用 commonpath 校验必须落在 PUBLIC 内，
   阻断 ../ 与 %2e%2e 之类的编码穿越。
3) 脚本上传：文件名白名单正则 + 长度上限 + 内容大小上限，拒绝二进制内容。
4) 请求体有大小上限，超限返回 413，避免内存打满。
5) 写操作落审计日志（var/audit.log），便于事后追溯。
6) 所有硬件写操作、脚本接口、配置修改和重启统一设门：
   未配置 TOKEN 时只允许本机调用，配置后凭口令调用。
   —— 否则「没设口令」等于把整台设备的 root shell 挂在局域网里。
7) POST /login 签发无固定有效期的会话，浏览器按来源持久保存，仅通过请求头发送。
   同一来源 5 分钟内错 10 次锁 5 分钟，锁住期间正确口令也不认。
"""

import hmac
import gzip
import hashlib
import difflib
import signal
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hwctl  # noqa: E402
import hwinfo  # noqa: E402  （只读本地兜底路径用：非 root 且无 agent 时的 /api/hwinfo）
import s2security
import s2notify
import s2risk
from s2validation import Problem, ALL_SCOPES, integer
import alerts  # noqa: E402  （硬件告警：后台线程定时检查，记录给右上角铃铛）

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.realpath(os.path.join(HERE, "public"))
PORT = int(os.environ.get("PORT", "18899"))
TOKEN = os.environ.get("TOKEN", "") or ""
AGENT = os.environ.get("S2_AGENT_URL", "http://127.0.0.1:8899")
VAR_DIR = os.environ.get("S2_VAR_DIR", "/var/apps/fnOS-S2/var")
SCRIPTS_DIR = os.path.join(VAR_DIR, "scripts")
AUDIT_LOG = os.path.join(VAR_DIR, "audit.log")
# 出厂默认维护脚本。事实来源只有这一份：cmd/install_callback 安装时复制它，
# /api/script/defaults 恢复默认时也复制它。以前安装回调用 heredoc 另写了一份，
# 改了那边的修复命令、这边还是旧的 —— 两份脚本迟早漂移。
DEFAULT_SCRIPTS_DIR = os.path.join(HERE, "default_scripts")

# 能直接拿到 root 的接口：脚本内容由用户决定，等价于任意命令执行。
# defaults 同样写可执行脚本，和 upload/save 同级门禁。
DANGER_PATHS = ("/api/script/save", "/api/script/start", "/api/script/upload",
                "/api/script/delete", "/api/script/defaults", "/api/restart")
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

# 所有硬件写操作均通过敏感操作授权；只读查询可以保留无口令访问。
READ_OPS = ("status", "hwinfo", "logs")
_NOTIFIER = None
_NOTIFIER_LOCK = threading.Lock()


def get_notifier():
    global _NOTIFIER
    with _NOTIFIER_LOCK:
        if _NOTIFIER is None:
            directory = os.environ.get("S2_OPERATIONS_DIR") or os.path.join(VAR_DIR, "operations")
            _NOTIFIER = s2notify.Notifier(directory, lambda: ALERTS.snapshot(200))
        return _NOTIFIER

# 电源操作的一次性确认码：nonce -> (action, 过期时刻)。见 Handler._power。
# 重启和脚本执行同级门禁（_danger_ok）：没配口令时只认本机，配了口令凭口令。
# 局域网里任何一台机器、任何一个人都能把 NAS 关掉，这不是「家用便利」能接受的代价。
# 门禁之外再加确认码 + 强制 JSON 头 + 审计，防误点和跨站。
POWER_NONCES = {}
POWER_LOCK = threading.Lock()
POWER_NONCE_TTL = 60
POWER_DRYRUN = os.environ.get("S2_POWER_DRYRUN") == "1"

# 口令试错限速：同一来源 FAIL_WINDOW 秒内错 FAIL_MAX 次，锁 FAIL_WINDOW 秒。
# 只按来源 IP 计，不按口令计；锁住期间连正确口令也不认，否则限速形同虚设。
FAIL_MAX = 10
FAIL_WINDOW = 300
AUTH_FAILS = {}            # ip -> [失败时刻, ...]
AUTH_FAIL_LOCK = threading.Lock()
COOKIE_NAME = "s2token"  # 仅用于清理旧版本 Cookie，不再接受它认证。
SESSIONS = s2security.Sessions(path=os.path.join(VAR_DIR, "auth-sessions.json"), binding=TOKEN)

# 未配置口令时，远程来源只能查询硬件；启用口令并重启后可登录写入。
DANGER_DENY_MSG = (
    "当前未设置访问口令：硬件控制、脚本执行和配置修改仅允许在设备本机操作。"
    "请到飞牛「应用设置 → 配置」设置访问口令并保存，"
    "应用重启后刷新本页，输入口令即可。"
)
POWER_DENY_MSG = (
    "当前未设置访问口令：重启仅允许在设备本机操作。"
    "请到飞牛「应用设置 → 配置」设置访问口令。"
)


# ---- 资源与安全阈值 ----
MAX_BODY = 512 * 1024            # 单次请求体上限 512KB
MAX_SCRIPT_BYTES = 256 * 1024    # 单个上传脚本上限 256KB
# .run 是「shell 头 + tar.gz 负载」的自解压包，里面装着模块的脚本、service、
# udev 规则，几百 KB 起步。拿 .sh 的 256KB 卡它，稍微像样点的包就传不进去。
MAX_RUN_BYTES = 4 * 1024 * 1024  # 单个 .run 安装包上限 4MB
MAX_LINES_PER_JOB = 2000         # 每个任务保留的最大输出行数
MAX_JOBS = 32                    # 同时保留的任务数上限
JOB_TTL = 3600                   # 任务记录保留秒数
AGENT_TTL = 30                   # agent 可用性探测结果缓存秒数
# agent 探测超时。原来写的是 2 秒：agent 装了但卡住（accept 了不回应）时，
# 每 30 秒就有一个请求要白等 2 秒，而前端是 10 秒轮询一次 —— 每三次轮询
# 撞上一次卡顿，观感就是「页面偶尔顿一下」。回环连接要么几毫秒通，要么
# 立刻被拒，0.6 秒足够。
AGENT_PING_TIMEOUT = 0.6

# 脚本文件名白名单：中英文、数字、下划线、短横线、点；禁止路径分隔符与 ..
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fa5]{1,64}$")

JOBS = {}
JOB_LOCK = threading.Lock()
_GZIP_CACHE = {}
_AUDIT_QUIET = {}
_AGENT_STATE = {"ok": None, "ts": 0.0}
_AGENT_LOCK = threading.Lock()
_AGENT_PROBE_LOCK = threading.Lock()   # 让并发请求排队探测，别一起去 ping


# ---------------- pid ----------------
def write_pid():
    p = os.environ.get("S2_PID_FILE")
    if p:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass


# ---------------- 审计 ----------------
def audit(action, detail, remote, ok):
    """写操作审计：单行 JSON，超过 1MB 自动截断重开，失败绝不影响主流程。"""
    try:
        os.makedirs(VAR_DIR, exist_ok=True)
        try:
            if os.path.getsize(AUDIT_LOG) > 1024 * 1024:
                os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
        except OSError:
            pass
        rec = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ip": remote,
            "action": action,
            "ok": bool(ok),
            "detail": detail,
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------- 硬件操作分发 ----------------
def agent_call(op, params):
    try:
        key = s2security.read_agent_key()
        if not key:
            return {"ok": False, "msg": "硬件代理凭据未配置，请重新部署代理"}
        req = urllib.request.Request(
            AGENT + "/hw",
            data=json.dumps({"op": op, "params": params}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-S2-Agent-Key": key})
        # timeout 是 urlopen 的参数，不是 Request 的 —— 写在 Request 上会抛 TypeError，
        # 非 root 安装下每一个经代理的写操作都会变成「硬件代理不可用」。
        # 定时开关机 apply 最长 30 秒，留出余量。
        with urllib.request.urlopen(req, timeout=200 if op == "patch" else 45) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa
        # 异常文本（ConnectionRefusedError 等）是英文、给排障用，不直接当提示语——
        # HW_SPEC §4：提示语用中文，原文进 detail。
        return {"ok": False, "msg": "硬件代理不可用", "detail": str(e)}


def agent_available():
    """带 TTL 的 agent 可用性探测：避免一次性判定失败后永久只读，
    也避免每个请求都去 ping。"""
    now = time.time()
    with _AGENT_LOCK:
        if _AGENT_STATE["ok"] is not None and now - _AGENT_STATE["ts"] < AGENT_TTL:
            return _AGENT_STATE["ok"]
    # 探测本身不能持 _AGENT_LOCK（那是网络 IO，会把所有等缓存的请求一起堵住），
    # 所以另用一把探测锁做双检：TTL 到期时只有第一个请求真的去 ping，
    # 后面排队的进来一看缓存已经刷新，直接返回。
    # 原来没有这把锁，开三个标签页就是三次并发 ping。
    with _AGENT_PROBE_LOCK:
        with _AGENT_LOCK:
            if _AGENT_STATE["ok"] is not None and time.time() - _AGENT_STATE["ts"] < AGENT_TTL:
                return _AGENT_STATE["ok"]
        ok = False
        try:
            key = s2security.read_agent_key()
            if not key:
                raise ValueError("agent key unavailable")
            req = urllib.request.Request(AGENT + "/ping", headers={"X-S2-Agent-Key": key})
            with urllib.request.urlopen(req, timeout=AGENT_PING_TIMEOUT) as r:
                info = json.loads(r.read().decode("utf-8"))
                ok = (r.status == 200 and info.get("root") is True
                      and info.get("hwctl") is True and info.get("auth") == 1)
        except Exception:
            ok = False
        with _AGENT_LOCK:
            _AGENT_STATE["ok"] = ok
            _AGENT_STATE["ts"] = time.time()
        return ok


def _shape_hw_result(ok, data):
    """把 hwctl.dispatch() / agent 返回的 (ok, data) 整理成 {ok, msg, data, detail}。

    data 有三种形态：
      · 字符串      —— 纯中文消息，直接进 msg，detail 留空
      · msg_detail  —— hwctl.msg_detail() 包的 {msg, detail}（HW_SPEC §4：中文提示
                        + 原始英文详情分离），拆成两个顶层字段
      · 其它字典     —— 真正的数据负载（collect_status()/collect_hwinfo() 等），
                        进 data，msg 留空
    """
    if isinstance(data, str):
        return {"ok": ok, "msg": data, "data": None, "detail": ""}
    if hwctl.is_msg_detail(data):
        return {"ok": ok, "msg": data.get("msg", ""), "data": None, "detail": data.get("detail", "")}
    return {"ok": ok, "msg": "", "data": data, "detail": ""}


def hw_call(op, params):
    if hwctl.is_root():
        ok, data = hwctl.dispatch(op, params)
        return _shape_hw_result(ok, data)
    if agent_available():
        r = agent_call(op, params)
        # agent_call() 已经是 {ok, msg, data} 形状（s2hw-agent.py 自己拼的），
        # 但它不认识 msg_detail 标记（旧版 agent 脚本不会解包），所以这里兜底：
        # 如果 agent 转发回来的 data 恰好是 msg_detail 包（新版 hwctl 在远端产生的），
        # 同样拆开，保持两条路径（直控 / agent 代理）对前端的返回形状一致。
        if hwctl.is_msg_detail(r.get("data")):
            d = r["data"]
            r = dict(r, msg=d.get("msg", ""), data=None, detail=d.get("detail", ""))
        return r
    if op == "status":
        return {"ok": True, "msg": "只读模式（未检测到管理员权限代理）", "data": hwctl.collect_status()}
    if op == "hwinfo":
        # 非 root、无 agent：本地只读兜底，和 status 的 readonly 分支同一个模式。
        # SMART 这部分读不到（需要 root），collect_hwinfo() 内部已经会把每块盘的
        # smart 置空并在 smart_err 里写明原因，这里不用特殊处理。
        force = bool(params.get("force"))
        return {"ok": True, "msg": "只读模式（未检测到管理员权限代理，缺少健康检测数据）",
                "data": hwinfo.collect_hwinfo(force=force)}
    if op == "schedule_info":
        return _shape_hw_result(*hwctl.dispatch(op, params))
    return {"ok": False, "msg": "当前为普通用户身份且未部署管理员权限代理，无法写入硬件；"
                               "请以管理员权限部署 s2hw-agent，或在应用中心改用管理员权限安装。",
            "detail": ""}


# ---------------- 硬件告警 ----------------
# 记录存在 var/alerts.json（升级 / 重启都保留）。检查线程只在 main() 里启动，
# 测试 import server 时不会多跑一个线程。
ALERTS = alerts.AlertStore(os.path.join(VAR_DIR, "alerts.json"))
_ALERT_WATCHER = None


def _alert_collect():
    """告警线程取数：传感器快照直接读本机（只读 /proc /sys，不需要 root）；
    SMART / 每盘容量走 hw_call("hwinfo")，非 root 时经代理拿到 SMART，自带 10 分钟缓存；
    服务状态在 collect_sensor 里（15 秒缓存）。"""
    sensor = hwctl.collect_sensor()
    r = hw_call("hwinfo", {})
    hw = r.get("data") if r.get("ok") else None
    return sensor, hw, sensor.get("services") or {}


# ---------------- 脚本任务 ----------------
def _job_done(k, v):
    p = v.get("proc")
    return p is None or p.poll() is not None


def cleanup_jobs(force=False):
    """清理已结束且超期的任务（内存表 + var/jobs 下的文件），并限制总数。"""
    now = time.time()
    with JOB_LOCK:
        for k in [k for k, v in JOBS.items() if _job_done(k, v) and now - v["t"] > JOB_TTL]:
            JOBS.pop(k, None)
        if len(JOBS) > MAX_JOBS:
            done = sorted((v["t"], k) for k, v in JOBS.items() if _job_done(k, v))
            for _, k in done[:len(JOBS) - MAX_JOBS]:
                JOBS.pop(k, None)
        live = set(JOBS)
    for k in [k for k in list(_LINE_COUNTS) if k not in live]:
        _LINE_COUNTS.pop(k, None)
    # 文件按修改时间清：跑完超过 JOB_TTL 的删掉。还在写的日志 mtime 一直是新的，不会误删
    try:
        for fn in os.listdir(JOBS_DIR):
            jid = fn.split(".", 1)[0]
            if jid in live or not JOB_ID_RE.match(jid):
                continue
            fp = os.path.join(JOBS_DIR, fn)
            try:
                if now - os.path.getmtime(fp) > JOB_TTL:
                    os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass
    if force:
        return len(JOBS)
    return None


def _find_bash():
    """定位 bash 解释器。

    设备上是 /bin/bash，直接能命中；Windows 本地测试时 bash 常常不在 PATH
    （Git for Windows 默认不加入），所以额外探测常见安装位置。
    也可用环境变量 S2_BASH 显式指定。
    """
    cands = [os.environ.get("S2_BASH") or ""]
    w = shutil.which("bash")
    if w:
        cands.append(w)
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
            cands.append(os.path.join(base, "Git", "usr", "bin", "bash.exe"))
            cands.append(os.path.join(base, "Programs", "Git", "bin", "bash.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _bash_env(bash):
    """Windows 下若用 Git Bash，把它的 msys bin 目录补进 PATH，
    否则脚本里的 sleep / ls 等 coreutils 会找不到。

    S2_PATH_PREPEND 是给本地沙箱垫片用的（tests/local_run.py 放一个 id 桩，
    让 .run 安装包能过 root 自检）。它必须排在 Git 的 bin **之前**，否则真 id
    会把垫片盖住；真机不设这个变量，行为完全不变。
    """
    env = os.environ.copy()
    pre = [d for d in env.get("S2_PATH_PREPEND", "").split(os.pathsep)
           if d and os.path.isdir(d)]
    cur = env.get("PATH", "")
    if os.name != "nt" or not bash:
        if pre:
            env["PATH"] = os.pathsep.join(pre + ([cur] if cur else []))
        return env
    p = os.path.normpath(bash)
    root = None
    for tail in (os.path.join("usr", "bin", "bash.exe"),
                 os.path.join("bin", "bash.exe")):
        if p.lower().endswith(os.sep + tail.lower()):
            root = p[: -len(tail) - 1]
            break
    if not root:
        if pre:
            env["PATH"] = os.pathsep.join(pre + ([cur] if cur else []))
        return env
    extra = [d for d in (os.path.join(root, "usr", "bin"),
                         os.path.join(root, "bin")) if os.path.isdir(d)]
    parts = pre + extra + ([cur] if cur else [])
    if parts:
        env["PATH"] = os.pathsep.join(parts)
    return env


def _bash_path(path):
    """给 bash 的脚本路径。

    Windows 上 os.path.join 出来的是反斜杠，而 bash（Git for Windows 的 MSYS）
    把 \\ 当转义符：C:\\var\\a.sh 会被读成 C:vara.sh，报「No such file or directory」。
    这里统一换成正斜杠；POSIX 设备上原样返回，不做任何多余的事。
    """
    if os.name != "nt":
        return path
    return os.path.abspath(path).replace("\\", "/")


# 任务落盘：var/jobs/<id>.log（输出）、.rc（退出码，跑完才出现）、.pid
# 以前任务只存在内存里，脚本运行期间控制台服务一重启（脚本自己重启了相关服务、
# 应用中心拉起 / 升级、进程被杀），任务表就没了，页面拿到 rc=None，显示「退出码 null」，
# 而脚本其实还在跑或已经跑完。现在脚本在独立会话里跑、输出直接写文件，
# 控制台重启不影响脚本，重启后照样能接着读到输出和退出码。
JOBS_DIR = os.path.join(VAR_DIR, "jobs")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# 终端颜色 / 光标控制码（\e[1;32m 之类）：网页日志不是终端，原样显示就是一串乱码
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")


def _job_files(job):
    b = os.path.join(JOBS_DIR, job)
    return b + ".log", b + ".rc", b + ".pid"


def clean_line(s):
    s = ANSI_RE.sub("", s)
    # 进度条用 \r 回到行首覆盖：只留最后一次的内容
    if "\r" in s:
        s = s.rstrip("\r").split("\r")[-1]
    return "".join(ch for ch in s if ch == "\t" or ch >= " " or ch > "\x7f")


def _pid_alive(pid):
    if not pid:
        return False
    if os.name != "posix":
        # Windows 上 os.kill(pid, 0) 不是「探测」而是 TerminateProcess，会把脚本杀掉。
        # 本地联调才会走到这里：用 OpenProcess + GetExitCodeProcess 查。
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259           # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, SystemError, ValueError):
        return False


def _pid_reused(pid, pidf):
    """设备重启后 PID 会被别的进程占用：那个进程比 .pid 文件还晚启动，就不是当初的任务。
    否则任务永远显示「运行中」，页面每次打开都接回它，运行按钮一直灰着。"""
    if os.name != "posix":
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])      # 第 22 项 starttime
        with open("/proc/stat") as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK") > os.path.getmtime(pidf) + 2
    except (OSError, ValueError, IndexError, StopIteration):
        return False


def read_job(job):
    """任务快照 {lines, done, rc, lost}。内存里没有（控制台重启过）就从 var/jobs 读。"""
    log, rcf, pidf = _job_files(job)
    with JOB_LOCK:
        mem = JOBS.get(job)
    if mem is not None and "rc" in mem:          # 没跑起来的任务（找不到 bash 等）
        pre = list(mem.get("pre") or [])
        return {"lines": pre, "total": len(pre), "done": True, "rc": mem["rc"], "lost": False}
    if mem is None and not os.path.exists(log):
        return {"lines": [], "total": 0, "done": True, "rc": None, "lost": True}
    size = 0
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            raw = f.read()
    except OSError:
        raw = b""
    raw_lines = raw.decode("utf-8", "replace").split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    tail_count = len(raw_lines)
    lines = [clean_line(x) for x in raw_lines[-MAX_LINES_PER_JOB:]]
    rc, done = None, False
    try:
        with open(rcf, "r") as f:
            txt = f.read()
        if txt.endswith("\n") and txt.strip().isdigit():   # 整行写完才算
            rc, done = int(txt.strip()), True
    except OSError:
        pass
    if not done:
        proc = mem.get("proc") if mem else None
        if proc is not None:
            code = proc.poll()
            if code is not None:
                # 外壳退出了却没写 .rc：脚本被杀（kill -9 / OOM），外壳也一起没了
                time.sleep(0.05)
                if os.path.exists(rcf):
                    return read_job(job)
                done, rc = True, (code if code else 137)
        else:
            try:
                with open(pidf) as f:
                    pid = int(f.read().strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if not _pid_alive(pid) or _pid_reused(pid, pidf):
                done, rc = True, None     # 控制台重启前就已经结束、又没留下退出码
    if done:
        _after_job(job, mem, rc)
    # 日志超过 256KB 时只读了尾部：总行数按整个文件的换行数算，页面才不会把新行当旧行吞掉。
    # 只数上次之后新增的那一段，别每 700ms 把整份日志从头读一遍。
    total = tail_count
    if size > len(raw):
        prev_size, prev_total = _LINE_COUNTS.get(job, (0, 0))
        start = prev_size if 0 < prev_size <= size else 0
        try:
            with open(log, "rb") as f:
                f.seek(start)
                counted, left = 0, size - start
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    counted += chunk.count(b"\n"); left -= len(chunk)
            total = (prev_total if start else 0) + counted
            if left == 0:
                _LINE_COUNTS[job] = (size, total)
            # 和小日志那条路口径一致：没写完换行的最后一行也算一行，跨过 256KB 时总数才不会倒退
            if raw and not raw.endswith(b"\n"):
                total += 1
        except OSError:
            pass
    return {"lines": lines, "total": total, "done": done, "rc": rc,
            "lost": done and rc is None}


_LINE_COUNTS = {}
_JOBS_NOTED = set()


def _after_job(job, mem, rc):
    """任务结束后只做一次的收尾：内置模块的原包从脚本库跑完，记下 / 清掉设备上装的版本。"""
    if job in _JOBS_NOTED:
        return
    _JOBS_NOTED.add(job)
    meta = mem or {}
    if "package" not in meta:
        try:
            with open(os.path.join(JOBS_DIR, job + ".meta"), encoding="utf-8") as stream:
                meta = json.load(stream)
        except (OSError, ValueError):
            return
    pkg = meta.get("package") if isinstance(meta, dict) else None
    if isinstance(pkg, dict):
        try:
            hwctl.note_package_run(pkg, meta.get("name", ""), rc == 0)
        except (OSError, ValueError):
            pass


def start_job(path, timeout=600, expected_hash=None):
    cleanup_jobs()
    job = uuid.uuid4().hex
    with open(path, "rb") as source:
        content = source.read(MAX_RUN_BYTES + 1)
    if len(content) > MAX_RUN_BYTES or (expected_hash and hashlib.sha256(content).hexdigest() != expected_hash):
        raise Problem("脚本内容已改变或过大，请重新审查", 409)
    original = path
    log, rcf, pidf = _job_files(job)
    metadata = {"name": os.path.basename(original), "timeout": timeout, "sha256": hashlib.sha256(content).hexdigest()}
    if original.lower().endswith(".run"):
        # 脚本库里放的是不是内置模块的原包（任意版本）：跑完后据此记下设备上装的版本
        try:
            pkg = hwctl.identify_package(os.path.basename(original), content)
        except (OSError, ValueError):
            pkg = None
        if pkg:
            metadata["package"] = pkg
    log_created = False
    def fail(lines, rc):
        # Keep launch failures visible, including after the web server restarts.
        with JOB_LOCK:
            JOBS[job] = {"t": time.time(), "pre": lines, "proc": None, "rc": rc, **metadata}
        if log_created:
            try:
                s2security.atomic_text(log, "\n".join(lines) + "\n")
                s2security.atomic_text(rcf, str(rc) + "\n")
                s2security.atomic_text(os.path.join(JOBS_DIR, job + ".meta"), json.dumps(metadata))
            except OSError:
                pass  # The in-memory record still explains a full/unwritable job directory.
        return job

    try:
        if os.path.islink(JOBS_DIR): raise OSError("任务目录不能是符号链接")
        os.makedirs(JOBS_DIR, mode=0o700, exist_ok=True)
        os.chmod(JOBS_DIR, 0o700)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        log_created = True
    except OSError as e:
        return fail(["无法创建任务日志：%s" % e], 1)

    bash = _find_bash()
    if not bash:
        return fail(["未找到 bash，无法执行脚本。",
                     "设备上应自带 /bin/bash；Windows 本地测试请安装 Git for Windows，",
                     "或用环境变量指定：set S2_BASH=C:\\Program Files\\Git\\bin\\bash.exe"], 127)
    env = _bash_env(bash)
    # .run 是自解压安装包，内部用 `id -u` 自检 root。本地沙箱的 id 垫片只在
    # 这个标记下返回 0；真机没有垫片，设了也不影响任何命令。
    if path.lower().endswith(".run"):
        env["S2_FAKE_ROOT"] = "1"
    # 独立 Python 监督进程负责超时、取消、退出码及临时快照清理。
    # User scripts keep relative-file semantics; bundled standalone tools must also work
    # when the application package itself is mounted read-only.
    snapshot_dir = JOBS_DIR if os.path.realpath(os.path.dirname(original)) == os.path.realpath(DEFAULT_SCRIPTS_DIR) else os.path.dirname(original)
    path = os.path.join(snapshot_dir, ".s2job-" + job + os.path.splitext(original)[1])
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    except OSError:
        return fail(["无法在脚本目录创建执行快照，请检查目录权限和可用空间"], 1)
    try:
        s2security.atomic_text(os.path.join(JOBS_DIR, job + ".meta"), json.dumps(metadata))
    except OSError:
        os.unlink(path)
        return fail(["无法保存任务记录，尚未执行脚本，请检查任务目录权限和可用空间"], 1)
    kw = {}
    if os.name == "posix":
        kw["start_new_session"] = True   # 脚本不跟着控制台进程组一起被杀
    try:
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "job_runner.py"), bash, _bash_path(path), log, rcf,
                              str(timeout), os.path.dirname(original)],
                             cwd=os.path.dirname(path), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError as e:
        os.unlink(path)
        return fail(["无法启动 bash：%s（%s）" % (bash, e)], 127)
    try:
        with open(pidf, "w") as f:
            f.write(str(p.pid))
    except OSError:
        pass
    with JOB_LOCK:
        JOBS[job] = {"t": time.time(), "proc": p, **metadata}
    return job


SCRIPT_START_LOCK = threading.Lock()


FACTORY_ACTIONS = {"00-硬件自检.sh", "10-收集诊断信息.sh", "20-升级S2设备树.sh", "30-安装电源灯美化.sh", "40-停用电源灯美化.sh", "50-回滚上次DTB升级.sh"}


def factory_script(name):
    if name not in FACTORY_ACTIONS: return None
    path = os.path.join(DEFAULT_SCRIPTS_DIR, name)
    return path if os.path.isfile(path) and not os.path.islink(path) else None


def script_review(name, value=None):
    # Retain only script review snapshots; no operations database or background writer.
    path = os.path.join(VAR_DIR, "script-reviews", hashlib.sha256(name.encode()).hexdigest() + ".json")
    if value is not None:
        try: s2security.atomic_text(path, json.dumps(value, ensure_ascii=False), mode=0o600)
        except (OSError, ValueError): pass  # Execution has already started; do not report it as unstarted.
        return value
    try:
        with open(path, encoding="utf-8") as stream: value = json.load(stream)
        return value if isinstance(value, dict) and isinstance(value.get("hash"), str) and isinstance(value.get("content"), str) else None
    except (OSError, ValueError): return None


def inspect_script(name, factory=False):
    path = factory_script(name) if factory else script_by_name(name)
    if not path:
        raise Problem("脚本不存在", 404)
    with open(path, "rb") as stream:
        content = stream.read(MAX_RUN_BYTES + 1)
    if len(content) > MAX_RUN_BYTES:
        raise Problem("脚本过大")
    digest = hashlib.sha256(content).hexdigest()
    preview = content[:65536].decode("utf-8", "replace")
    old = script_review(("factory:" if factory else "") + name)
    return {"name": name, "sha256": digest, "size": len(content), "content": preview,
            "factory": factory,
            "risks": s2risk.inspect(content, name, DEFAULT_SCRIPTS_DIR),
            "source": "随包内置工具" if factory else "应用脚本库" if os.path.dirname(path) == SCRIPTS_DIR else "共享目录",
            "changed": not old or old["hash"] != digest, "truncated": len(content) > 65536,
            "diff": "\n".join(difflib.unified_diff((old["content"] if old else "").splitlines(), preview.splitlines(),
                                                fromfile="上次执行", tofile="当前内容", lineterm=""))}


def managed_jobs():
    result = []
    try:
        if os.path.islink(JOBS_DIR) or not os.path.isdir(JOBS_DIR):
            filenames = []
        else:
            def mtime(name):
                try:
                    return os.path.getmtime(os.path.join(JOBS_DIR, name))
                except OSError:      # 清理线程刚好删掉这一个：排到最前，别让整份列表作废
                    return 0
            metas = [f for f in os.listdir(JOBS_DIR) if f.endswith(".meta")]
            # 按时间取最近 256 个：文件名是随机 uuid，按名字排序等于随机挑
            filenames = sorted(metas, key=mtime)[-256:]
    except OSError:
        filenames = []
    for filename in filenames:
        identifier = filename.split(".")[0]
        if not filename.endswith(".meta") or not JOB_ID_RE.fullmatch(identifier):
            continue
        try:
            with open(os.path.join(JOBS_DIR, filename), encoding="utf-8") as stream:
                metadata = json.load(stream)
            result.append({"id": identifier, **metadata, **read_job(identifier)})
        except (OSError, ValueError):
            continue
    seen = {item["id"] for item in result}
    with JOB_LOCK:
        memory = list(JOBS.items())
    for identifier, item in memory:
        if identifier not in seen and item.get("name"):
            result.append({"id": identifier, "name": item["name"], "timeout": item.get("timeout", 600), **read_job(identifier)})
    return result


def script_by_name(name):
    for s in hwctl.list_scripts():
        if s["name"] == name:
            return s["path"]
    return None


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "S2HW/1.2"
    protocol_version = "HTTP/1.1"          # 复用连接，减少轮询开销
    timeout = 30                           # 慢速/半开连接别无限期占线程
    # 小响应（按钮结果、轮询）不要等对端的延迟 ACK：否则每次多等 40–200ms
    disable_nagle_algorithm = True

    # public/ 只发这几种后缀。见 _static 里的说明。
    STATIC_EXT = (".html", ".js", ".css", ".svg", ".png", ".ico", ".webmanifest")

    # 口令登录页。刻意不含任何脚本（CSP 是 script-src 'self'，而这页要在脚本加载前出来）。
    # 用 POST 表单提交到 /login：GET 表单会把口令拼进地址（?token=），
    # 哪怕随后 302 掉，也已经留在浏览器历史和地址栏补全里了。
    # 配色与主界面同一套（浅色默认，深色跟系统），标识同顶栏。
    LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>NIX fnOS-S2 · 访问口令</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%232e78ff'/%3E%3Cstop offset='1' stop-color='%231034a8'/%3E%3C/linearGradient%3E%3CclipPath id='c'%3E%3Crect x='25' y='22' width='50' height='56' rx='6'/%3E%3C/clipPath%3E%3C/defs%3E%3Crect width='100' height='100' rx='23' fill='url(%23g)'/%3E%3Cpath d='M25 22 L40 22 L75 78 L60 78 Z' fill='%23fff' fill-opacity='0.62' clip-path='url(%23c)'/%3E%3Crect x='25' y='22' width='15' height='56' rx='6' fill='%23fff'/%3E%3Ccircle cx='32.5' cy='69' r='3.3' fill='%2334e68c'/%3E%3Crect x='60' y='22' width='15' height='56' rx='6' fill='%23fff'/%3E%3Ccircle cx='67.5' cy='69' r='3.3' fill='%2334e68c'/%3E%3C/svg%3E">
<style>
:root{--bg:#f3f5f8;--card:#fff;--line:#e1e6ec;--line-2:#d2d9e2;--fg:#1a2330;--muted:#5d6979;
 --accent:#1769e0;--on-accent:#fff;--crit:#d43d3d;--crit-soft:#fdecee;--field:#f7f9fb;
 --ring:rgba(23,105,224,.16);color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#10141a;--card:#171c23;--line:#29313c;--line-2:#36404d;
 --fg:#e3e8ee;--muted:#9ba6b3;--accent:#4b8cf5;--on-accent:#0b1220;--crit:#f06464;--crit-soft:#2b171b;
 --field:#1c222a;--ring:rgba(75,140,245,.26);color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;
 padding:24px max(16px,env(safe-area-inset-right,0px)) 24px max(16px,env(safe-area-inset-left,0px));
 background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased;
 font:14px/1.6 "Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Segoe UI",system-ui,sans-serif}
form{width:100%;max-width:360px;padding:28px 24px 24px;border-radius:10px;background:var(--card);
 border:1px solid var(--line);box-shadow:0 12px 32px -18px rgba(16,24,40,.35)}
.brand{display:flex;align-items:center;gap:10px;margin:0 0 20px}
.mark{width:32px;height:32px;border-radius:8px;display:grid;place-items:center;color:#fff;
 background:linear-gradient(180deg,#2e78ff,#1034a8)}
.mark svg{width:32px;height:32px}
.name .org{font-weight:700;letter-spacing:.06em;margin-right:5px}
.name{font-size:15px;font-weight:600}.name em{font-style:normal;color:var(--accent)}
.sub{font-size:11.5px;color:var(--muted);margin-left:2px}
h1{margin:0 0 4px;font-size:17px;font-weight:600}
p{margin:0 0 16px;font-size:13px;color:var(--muted)}
p.err{color:var(--crit);background:var(--crit-soft);padding:8px 10px;border-radius:6px;margin:0 0 14px}
label{display:block;font-size:12.5px;color:var(--muted);margin:0 0 6px}
/* 只留一道边框：聚焦时边框变色 + 内贴的柔光，不再在外面再套一圈 outline（看起来像双边框）。
   手机浏览器自动填充会把底色刷成黄/蓝，用内阴影盖回输入框底色。 */
input{display:block;width:100%;height:40px;margin:0;padding:0 12px;border-radius:6px;border:1px solid var(--line-2);
 background:var(--field);color:inherit;font:inherit;font-size:16px;outline:0;box-shadow:none;
 -webkit-appearance:none;appearance:none;transition:border-color .15s,box-shadow .15s}
input:focus,input:focus-visible{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
input:-webkit-autofill{-webkit-text-fill-color:var(--fg);caret-color:var(--fg);
 -webkit-box-shadow:0 0 0 40px var(--field) inset;box-shadow:0 0 0 40px var(--field) inset}
input:-webkit-autofill:focus{-webkit-box-shadow:0 0 0 40px var(--field) inset,0 0 0 3px var(--ring);
 box-shadow:0 0 0 40px var(--field) inset,0 0 0 3px var(--ring)}
input::-ms-reveal{filter:opacity(.6)}
button{margin-top:14px;width:100%;height:40px;border-radius:6px;border:0;background:var(--accent);
 color:var(--on-accent);font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
</style></head><body>
<form method="post" action="/login">
 <div class="brand"><span class="mark"><svg viewBox="0 0 100 100" aria-hidden="true">
  <clipPath id="nix-c"><rect x="25" y="22" width="50" height="56" rx="6"/></clipPath>
  <path d="M25 22 L40 22 L75 78 L60 78 Z" fill="currentColor" fill-opacity=".62" clip-path="url(#nix-c)"/>
  <rect x="25" y="22" width="15" height="56" rx="6" fill="currentColor"/>
  <rect x="60" y="22" width="15" height="56" rx="6" fill="currentColor"/>
  <circle cx="32.5" cy="69" r="3.6" fill="#34e68c"/><circle cx="67.5" cy="69" r="3.6" fill="#34e68c"/></svg></span>
  <span class="name"><b class="org">NIX</b><em>fnOS-S2</em></span><span class="sub">硬件控制台</span></div>
 <h1>输入访问口令</h1>
 <p>登录后长期记住，无固定有效期；退出登录或修改口令后需重新输入。</p>
 <p class="err" id="login-error" role="alert" hidden></p>
 {{ERR}}
 <label for="tk">访问口令</label>
 <input id="tk" type="password" name="token" autofocus required maxlength="128"
        autocomplete="current-password" {{DIS}}>
 <input type="hidden" name="next" value="{{NEXT}}">
 <button type="submit" {{DIS}}>进入控制台</button>
</form><script src="/auth.js" defer></script><script src="/login.js" defer></script></body></html>"""

    def log_message(self, fmt, *args):
        """默认实现会打印完整 requestline，?token= 就跟着进日志了。
        访问日志统一走 log_request，这里静音。"""
        return

    def log_request(self, code="-", size="-"):
        """只记路径不记 query，避免旧链接中的口令进入日志。"""
        # 请求行本身坏掉时 send_error 也会走到这里，那时 self.path 还没有
        path = urllib.parse.urlsplit(getattr(self, "path", "") or "").path or "/"
        sys.stderr.write('[%s] "%s %s %s" %s %s\n' % (
            time.strftime("%F %T"), self.command, path, self.request_version, code, size))


    # ---- helpers ----
    def _security_headers(self):
        if self._cookie_token():
            self.send_header("Set-Cookie", "s2token=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # 注意：这里刻意不加 X-Frame-Options / frame-ancestors。
        # 飞牛桌面可能以 iframe 方式打开本控制台（端口与桌面不同源），
        # 一旦设为 DENY/SAMEORIGIN 会导致「点桌面图标打不开」。
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 已经决定要断连接（超限请求体 / 协议错误）时必须在响应里写明，
        # 否则中间的代理会以为这条连接还能复用，继续把后续字节往里塞。
        if self.close_connection:
            self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """读取并解析 JSON 请求体；超限或非法时返回 (None, 错误响应码)。"""
        # 不接受分块传输：本服务不实现 chunked 解码，一旦放过就会把
        # 分块体原样留在缓冲区里，被当成下一条请求解析（和下面的 413 同一类问题）。
        if (self.headers.get("Transfer-Encoding", "") or "").strip().lower() not in ("", "identity"):
            self.close_connection = True
            return None, 400
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return None, 400
        if n < 0:
            self.close_connection = True
            return None, 400
        if n == 0:
            return {}, None
        if n > MAX_BODY:
            # 关键：这里【不读】请求体，所以必须断开连接。
            # protocol_version 是 HTTP/1.1，不主动断的话剩下那几十万字节会被
            # 当成下一条请求继续解析 —— 实测能在一个连接里收到两个响应，
            # 服务端日志里会出现 "AAAA…GET /api/state HTTP/1.1" 这种鬼东西。
            # 前后挂一层反向代理时，这就是标准的请求走私（CL.TE）。
            self.close_connection = True
            return None, 413
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, 400
        if not isinstance(obj, dict):
            return None, 400
        return obj, None

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _raw_body(self, limit=MAX_SCRIPT_BYTES):
        """读取原始字节请求体（脚本上传用）；超限或非法返回 (None, 错误码)。

        与 _body() 同样拒绝 chunked：不实现解码，放过就会把分块体留在缓冲区里
        被当成下一条请求解析。
        """
        if (self.headers.get("Transfer-Encoding", "") or "").strip().lower() not in ("", "identity"):
            self.close_connection = True
            return None, 400
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return None, 400
        if n <= 0:
            return None, 400
        if n > limit:
            # 超限时先把 body 读掉再回 413。直接断开的话客户端还在写，
            # 收到的是 RST 而不是响应 —— 用户只看到「连接被重置」，
            # 完全不知道是自己拖了个太大的文件。丢弃量封顶 8MB：
            # 再大的请求不值得为了一句错误提示读完。
            drop = min(n, 8 * 1024 * 1024)
            while drop > 0:
                chunk = self.rfile.read(min(65536, drop))
                if not chunk:
                    break
                drop -= len(chunk)
            self.close_connection = True
            return None, 413
        return self.rfile.read(n), None

    def _script_upload(self, remote):
        """接收脚本文件的原始字节（.sh 脚本，或 .run 单文件自解压模块包）。

        文件名走查询参数 ?name=xxx 而不是 JSON 包装 —— 前端可以直接把 File
        对象当 body 发过来，不必先 base64 撑大三分之一。
        """
        q = self._query()
        raw = str((q.get("name") or [""])[0] or "").strip()
        if not raw:
            self._json({"ok": False, "msg": "缺少文件名参数 name"}, 400)
            return
        if "/" in raw or "\\" in raw or ".." in raw:
            self._json({"ok": False, "msg": "文件名不能包含路径分隔符或 .."}, 400)
            return
        name = raw if raw.endswith((".sh", ".run")) else raw + ".sh"
        if not SCRIPT_NAME_RE.match(name):
            self._json({"ok": False, "msg": "文件名只能包含中英文、数字、下划线、短横线与点"}, 400)
            return
        # .run 是「shell 头 + tar.gz 负载」的自解压包，负载里必然有 NUL，
        # 拿文本脚本的标准卡它，等于让 UI 上那句「把 .run 拖进来」永远失败。
        # 所以两种文件各按各的形态校验，而不是统一按文本。
        is_run = name.endswith(".run")
        limit = MAX_RUN_BYTES if is_run else MAX_SCRIPT_BYTES
        data, err = self._raw_body(limit=limit)
        if err:
            self._json({"ok": False,
                        "msg": ("文件过大（上限 %d KB）" % (limit // 1024)
                                if err == 413 else "请求体格式错误")}, err)
            return
        if is_run:
            # 自解压包的第一行必须是 shebang：bash 靠它确认这是脚本，
            # 头部跑到 exit 之后就不再解析二进制负载。没有头的裸 tar 传进来
            # 只会在运行时得到一条「cannot execute binary file」。
            if not data.startswith(b"#!"):
                self._json({"ok": False,
                            "msg": ".run 缺少 #! 开头，不是自解压安装包"}, 400)
                return
        elif b"\x00" in data:
            # 二进制文件当 .sh 存进去只会得到一条莫名其妙的 bash 报错，提前拒掉
            self._json({"ok": False, "msg": "不是文本脚本（含 NUL 字节）"}, 400)
            return
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        target = os.path.join(SCRIPTS_DIR, name)
        tmp = target + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
        except OSError as e:
            self._json({"ok": False, "msg": "写入失败", "detail": str(e)}, 500)
            return
        audit("script:upload", {"name": name, "bytes": len(data)}, remote, True)
        self._json({"ok": True, "msg": "已上传 %s" % name,
                    "scripts": hwctl.list_scripts()})

    def _script_defaults(self, remote):
        """把出厂默认维护脚本补进脚本目录（恢复出厂脚本用）。

        页面已没有「加载默认脚本」按钮：出厂脚本由 cmd/install_callback 在装 FPK
        时写入，误删之后调这个接口补回，或重装 FPK。只补不覆盖 —— 用户改过的同名
        脚本必须原样留着，不能悄悄改回去。重复调用安全，第二次只会返回 skipped。
        """
        if not os.path.isdir(DEFAULT_SCRIPTS_DIR):
            self._json({"ok": False, "msg": "出厂默认脚本目录不存在（安装包可能不完整）"}, 500)
            return
        try:
            names = sorted(f for f in os.listdir(DEFAULT_SCRIPTS_DIR)
                           if f.endswith(".sh") and SCRIPT_NAME_RE.match(f))
        except OSError as e:
            self._json({"ok": False, "msg": "读取默认脚本失败", "detail": str(e)}, 500)
            return
        if not names:
            self._json({"ok": True, "added": [], "skipped": [], "scripts": hwctl.list_scripts(),
                        "msg": "内置自愈与灯光功能已整合到控制页的模块管理"})
            return

        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        added, skipped, failed = [], [], []
        for name in names:
            dst = os.path.join(SCRIPTS_DIR, name)
            if os.path.exists(dst):
                skipped.append(name)
                continue
            try:
                with open(os.path.join(DEFAULT_SCRIPTS_DIR, name), "rb") as f:
                    data = f.read()
                # 和上传走同一套形态校验：不是 shebang 文本脚本就别往里放
                if not data.startswith(b"#!") or b"\x00" in data or len(data) > MAX_SCRIPT_BYTES:
                    failed.append(name)
                    continue
                tmp = dst + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.chmod(tmp, 0o755)
                os.replace(tmp, dst)
                added.append(name)
            except OSError:
                failed.append(name)
        if failed:
            audit("script:defaults", {"failed": failed}, remote, False)
            self._json({"ok": False, "msg": "这些默认脚本写入失败：" + "、".join(failed)}, 500)
            return
        audit("script:defaults", {"added": added, "skipped": skipped}, remote, True)
        self._json({"ok": True, "added": added, "skipped": skipped,
                    "scripts": hwctl.list_scripts()})

    # ---- 口令 ----
    @staticmethod
    def _tok_eq(got):
        """常量时间比较。按字节比：hmac.compare_digest 对含非 ASCII 的 str 直接抛 TypeError，
        有人在登录框里输一个中文字符，整条连接就 500 了。"""
        if not got or not TOKEN:
            return False
        return hmac.compare_digest(str(got).encode("utf-8", "replace"), TOKEN.encode("utf-8"))

    def _remote(self):
        return self.client_address[0] if self.client_address else "-"

    def _locked(self):
        """这个来源最近试错太多次 → True。锁住时连正确口令也不认。"""
        if not TOKEN:
            return False
        ip, now = self._remote(), time.time()
        with AUTH_FAIL_LOCK:
            ts = [t for t in AUTH_FAILS.get(ip, ()) if now - t < FAIL_WINDOW]
            if ts:
                AUTH_FAILS[ip] = ts
            else:
                AUTH_FAILS.pop(ip, None)
            return len(ts) >= FAIL_MAX

    def _note_auth(self, ok):
        """一条请求只记一次（同一请求里 _auth_ok 可能被问两遍）。"""
        if getattr(self, "_auth_noted", False):
            return
        self._auth_noted = True
        ip = self._remote()
        with AUTH_FAIL_LOCK:
            if ok:
                AUTH_FAILS.pop(ip, None)
                return
            if len(AUTH_FAILS) > 256 and ip not in AUTH_FAILS:
                # 表别无限长：挤掉最久没动静的那个来源
                AUTH_FAILS.pop(min(AUTH_FAILS, key=lambda k: max(AUTH_FAILS[k] or [0])), None)
            AUTH_FAILS.setdefault(ip, []).append(time.time())
        if not ok:
            audit("auth:fail", {"ip": ip}, ip, False)

    def _cookie_token(self):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            part = part.strip()
            if part.startswith(COOKIE_NAME + "="):
                # 写入时做了 quote，这里必须 unquote 再比对，否则带特殊字符的口令永远对不上
                return urllib.parse.unquote(part.split("=", 1)[1].strip())
        return ""

    def _session_token(self):
        value = self.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    def _principal(self):
        if not hasattr(self, "_principal_cached"):
            self._principal_cached = self._resolve_principal()
        return self._principal_cached

    def _resolve_principal(self):
        token = self._session_token()
        if token.startswith("s2k_"):
            return None  # Automation keys belonged to the removed operations feature.
        if SESSIONS.valid(token):
            subject = SESSIONS.subject(token)
            if subject is not None:
                return None  # Legacy account sessions are no longer accepted.
            return {"name": "owner", "kind": "owner", "scopes": ALL_SCOPES}
        explicit = self.headers.get("X-S2-Token", "")
        if explicit and TOKEN:
            ok = self._tok_eq(explicit)
            self._note_auth(ok)
            if ok:
                return {"name": "owner", "kind": "owner", "scopes": ALL_SCOPES}
        if token or explicit:
            return None
        if not TOKEN:
            local = self._remote() in LOOPBACK
            return {"name": "local" if local else "guest",
                    "kind": "local" if local else "guest", "scopes": ALL_SCOPES if local else ["read"]}
        return None

    def _auth_ok(self):
        return self._principal() is not None

    def _require_scope(self, scope):
        principal = self._principal()
        if principal and scope in principal.get("scopes", []):
            return True
        self.close_connection = True
        self._json({"ok": False, "msg": "当前访问凭据没有此操作权限"}, 403)
        return False

    def _danger_ok(self, remote):
        principal = self._principal()
        return bool(principal and any(s in principal.get("scopes", []) for s in ("admin", "control", "scripts")))

    def _write_origin_ok(self):
        """写请求必须来自本站页面（防跨站提交）。

        浏览器给的 Sec-Fetch-Site 是按页面实际来源算的，经过反向代理也不变：有它就只看它。
        飞牛 App / 远程访问的代理会把 Host 改成设备内网地址，而 Origin 仍是外部域名，
        只拿 Origin 和 Host 比，手机上会一律被当成跨站拒绝（登录页报「请求来源不受信任」）。
        没有 Sec-Fetch-Site 的老浏览器才比 Origin，并认代理转发的原始主机名。"""
        site = self.headers.get("Sec-Fetch-Site", "")
        if site:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if (parsed.scheme not in ("http", "https") or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            return False
        hosts = {self.headers.get("Host", "").lower()}
        forwarded = self.headers.get("X-Forwarded-Host", "")
        if forwarded:
            hosts.add(forwarded.split(",")[0].strip().lower())
        return parsed.netloc.lower() in hosts

    @staticmethod
    def _safe_next(nxt):
        """登录后跳回的地址只允许本站相对路径，防开放重定向（//evil.com、http://…）。"""
        nxt = str(nxt or "/")
        if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or "\r" in nxt or "\n" in nxt:
            return "/"
        return nxt

    def _login_page(self, bad=False, locked=False, code=200):
        """公开口令登录页；随机会话按来源持久保存。"""
        if locked:
            note = '<p class="err">口令错误次数太多，请 5 分钟后再试。</p>'
        elif bad:
            note = '<p class="err">口令不正确，请重新输入。</p>'
        else:
            note = ""
        nxt = "/"
        body = (self.LOGIN_PAGE.replace("{{ERR}}", note)
                .replace("{{DIS}}", "disabled" if locked else "")
                .replace("{{NEXT}}", html_escape(nxt))).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _login_post(self):
        if self._locked():
            self.close_connection = True
            self._json({"ok": False, "msg": "口令错误次数太多，请 5 分钟后再试"}, 429)
            return
        if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/x-www-form-urlencoded":
            self.close_connection = True
            self._json({"ok": False, "msg": "请求格式不对"}, 415)
            return
        raw, err = self._raw_body(limit=4096)
        if err:
            self._json({"ok": False, "msg": "登录请求格式错误"}, err)
            return
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        username = (form.get("username") or [""])[0]
        password = (form.get("token") or [""])[0]
        if username:
            self._json({"ok": False, "msg": "无需账号，请只输入访问口令"}, 400)
            return
        owner = bool(TOKEN and self._tok_eq(password))
        anonymous = not TOKEN
        ok = owner or anonymous
        self._note_auth(ok)
        if not ok:
            self._json({"ok": False, "msg": "访问口令不正确"}, 401)
            return
        try:
            session = "" if anonymous else SESSIONS.issue()
        except (OSError, ValueError):
            self._json({"ok": False, "msg": "无法保存登录状态，请检查应用数据目录"}, 503)
            return
        audit("auth:login", {"actor": "owner"}, self._remote(), True)
        self._json({"ok": True, "session": session, "next": "/"})

    def _auth_fail_json(self):
        """API 401：前端清除会话并跳到登录页。"""
        self._json({"ok": False, "msg": "需要访问口令"}, 401)

    def _static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        rel = urllib.parse.unquote(path.lstrip("/").split("?")[0])
        full = os.path.realpath(os.path.join(PUBLIC, rel))
        # 归一化后再校验必须位于 PUBLIC 内，阻断 ../ 与编码穿越。
        # Windows 下若 rel 自带盘符（C:/x），commonpath 会抛 ValueError，
        # 那同样是「不在 PUBLIC 内」，按穿越处理即可，别让它变成 500。
        try:
            inside = os.path.commonpath([full, PUBLIC]) == PUBLIC
        except ValueError:
            inside = False
        # 后缀白名单 + 找不到就 404。
        # 以前「找不到就回落 index.html 并返回 200」，等于把 public/ 变成一个
        # 任何路径都答 200 的文件服务器：既分不清「真的不存在」和「首页」，
        # 也让 public/ 里任何被顺手扔进去的东西都自动对外可读。
        if (rel not in ("index.html", "app.js", "style.css", "auth.js", "login.js", "notifications.html", "notifications.js", "notifications.css")
                or not inside or not os.path.isfile(full)
                or not full.endswith(self.STATIC_EXT)):
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8"
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".png"):
            ctype = "image/png"
        elif full.endswith(".svg"):
            ctype = "image/svg+xml"
        try:
            st = os.stat(full)
        except OSError:
            self.send_error(404)
            return
        # ETag 用 mtime+size 拼出来，不读文件内容算哈希——这几个静态文件在手机上
        # 每次打开页面都会被请求一次（app.js 近百 KB），命中缓存就能把这次请求
        # 从「发整个文件」降到「只有响应头」，手机流量和设备出口带宽都省下来。
        # Cache-Control 仍是 no-cache：不是不校验，是校验完发现没变就不必重发正文。
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        last_mod = self.date_time_string(st.st_mtime)
        if_none_match = (self.headers.get("If-None-Match") or "").strip()
        if_mod_since = self.headers.get("If-Modified-Since")
        not_modified = (if_none_match and if_none_match == etag) or \
            (not if_none_match and if_mod_since and if_mod_since == last_mod)
        if not_modified:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        # 页面三件套未压缩有 300KB 以上，文本压缩后不到三分之一；按 (文件, ETag) 缓存压缩结果，
        # 每个版本只压一次，不占每次请求的 CPU。
        gzipped = False
        if len(data) > 1024 and full.endswith((".html", ".js", ".css", ".svg")) \
                and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            key = (full, etag)
            packed = _GZIP_CACHE.get(key)
            if packed is None:
                packed = gzip.compress(data, 6)
                if len(_GZIP_CACHE) > 32:
                    _GZIP_CACHE.clear()
                _GZIP_CACHE[key] = packed
            data, gzipped = packed, True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _power(self, path, body, remote):
        """立即重启。两步：
          1. POST /api/power/prepare {action}  → 一次性确认码（60 秒有效）
          2. POST /api/power {action, nonce} → 执行
        前端在确认弹窗打开时取码、用户点「确认」时才发第 2 步，这是服务端这一侧的二次确认：
        旧页面残留的请求、重复提交都会因为码过期或已用掉而被拒。
        另外要求 Content-Type: application/json —— 跨站网页的表单 / 简单请求带不了这个头
        （带了就要预检，本服务不应答预检），局域网里某个恶意网页没法替用户点「重启」。"""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"ok": False, "msg": "请求格式不对"}, 415)
            return
        action = str(body.get("action") or "")
        if action not in hwctl.POWER_ACTIONS:
            self._json({"ok": False, "msg": "未知的电源操作"}, 400)
            return
        now = time.time()
        with POWER_LOCK:
            for k in [k for k, v in POWER_NONCES.items() if v[1] < now]:
                POWER_NONCES.pop(k, None)
            if path == "/api/power/prepare":
                if len(POWER_NONCES) >= 16:
                    POWER_NONCES.pop(min(POWER_NONCES, key=lambda k: POWER_NONCES[k][1]), None)
                nonce = uuid.uuid4().hex
                POWER_NONCES[nonce] = (action, now + POWER_NONCE_TTL)
                self._json({"ok": True, "nonce": nonce, "ttl": POWER_NONCE_TTL})
                return
            got = POWER_NONCES.pop(str(body.get("nonce") or ""), None)
        if not got or got[0] != action:
            audit("power:%s" % action, {"ip": remote, "why": "nonce"}, remote, False)
            self._json({"ok": False, "msg": "确认已过期，请重新操作"}, 409)
            return
        params = {"action": action}
        if POWER_DRYRUN:
            # 本地测试用：走完整流程但不执行（真机不会设置这个环境变量）
            r = {"ok": True, "dryrun": True,
                 "msg": "（演练）设备正在重启"}
        else:
            r = hw_call("power", params)
        audit("power:%s" % action, params, remote, r.get("ok"))
        self._json(r)

    @staticmethod
    def _norm(path):
        """把 /api/v1/xxx 归一到 /api/xxx，保留旧路径兼容。"""
        if path.startswith("/api/v1/"):
            return "/api/" + path[len("/api/v1/"):]
        return path

    # ---- GET ----
    def _guarded(self, handler):
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:  # noqa: BLE001 — one bad request must not drop the connection silently
            sys.stderr.write("[%s] unhandled %s %s: %r\n" % (
                time.strftime("%F %T"), self.command, urllib.parse.urlsplit(self.path).path, error))
            self.close_connection = True
            try:
                self._json({"ok": False, "msg": "服务器内部错误，请稍后重试", "detail": type(error).__name__}, 500)
            except Exception:  # noqa: BLE001 — headers may already be on the wire
                pass

    def do_GET(self):
        self._guarded(self._do_get)

    def do_POST(self):
        self._guarded(self._do_post)

    def _do_get(self):
        if hasattr(self, "_principal_cached"):
            del self._principal_cached
        raw_path = urllib.parse.urlsplit(self.path).path
        if not raw_path.startswith("/api/"):
            if "token" in self._query():
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self._security_headers()
                self.end_headers()
                return
            if raw_path == "/login":
                self._login_page()
            else:
                # 只开放应用壳与明确列出的静态资源，设备数据全部经过 API 鉴权。
                self._static(raw_path)
            return
        path = self._norm(raw_path)
        if self._locked():
            self._json({"ok": False, "msg": "口令错误次数太多，请 5 分钟后再试"}, 429)
            return
        if not self._auth_ok():
            self._auth_fail_json()
            return
        if path.startswith("/api/ops/"):
            self._json({"ok": False, "msg": "接口已移除"}, 404)
            return
        if path == "/api/notifications":
            if not self._require_scope("admin"): return
            self._json({"ok": True, "data": get_notifier().notifications()})
            return
        if not self._require_scope("scripts" if path.startswith("/api/script/") else "read"):
            return
        q = self._query()
        if path == "/api/state":
            r = hw_call("status", {})
            # 页面据此决定重启 / 改端口这些按钮能不能点、该提示什么
            r["access"] = {"token": bool(TOKEN), "danger": self._danger_ok(self._remote()),
                           "scopes": self._principal().get("scopes", []), "actor": self._principal()["name"]}
            self._json(r)
        elif path == "/api/sensor":
            # 轻量快照：前端 10 秒轮询走这里，不读配置、不扫脚本/模块目录
            self._json({"ok": True, "data": hwctl.collect_sensor()})
        elif path == "/api/hwinfo":
            # 慢变硬件信息：走 hw_call 而不是直接调 hwinfo.collect_hwinfo()，
            # 这样非 root 安装能经由 root 常驻的 s2hw-agent 代采 SMART
            # （SMART 需要 root；见 HW_SPEC 第 1 节）。鉴权与 /api/state 同一道。
            force = (q.get("force") or ["0"])[0] in ("1", "true", "yes")
            self._json(hw_call("hwinfo", {"force": force}))
        elif path == "/api/alerts":
            # 铃铛：未读数 + 最近的事件 + 仍在持续的问题。只读本地 JSON，不碰硬件
            try:
                limit = max(1, min(200, int((q.get("limit") or ["100"])[0])))
            except ValueError:
                limit = 100
            snap = ALERTS.snapshot(limit)
            snap["watching"] = bool(_ALERT_WATCHER and _ALERT_WATCHER.is_alive())
            self._json({"ok": True, "data": snap})
        elif path == "/api/log":
            name = (q.get("name") or ["sata-led"])[0]
            self._json(hw_call("logs", {"name": name}))
        elif path == "/api/script/inspect":
            try:
                self._json({"ok": True, "data": inspect_script((q.get("name") or [""])[0], q.get("factory") == ["1"])})
            except Problem as error:
                self._json({"ok": False, "msg": str(error)}, error.status)
        elif path == "/api/script/jobs":
            self._json({"ok": True, "data": managed_jobs()})
        elif path == "/api/script/get":
            name = os.path.basename((q.get("name") or [""])[0])
            p = script_by_name(name)
            if not p:
                self._json({"ok": False, "msg": "脚本不存在"}, 404)
                return
            try:
                # .run 是「shell 头 + tar.gz 负载」的自解压包。整个按文本读会把
                # 二进制倒给「查看」，而且 4MB 的包会被下面 256KB 的文本上限
                # 误判成过大。只取负载之前的可读行，剩下的如实说明是二进制。
                if name.endswith(".run"):
                    with open(p, "rb") as f:
                        raw = f.read(65536)
                    lines = []
                    for line in raw.split(b"\n"):
                        # 允许 tab / LF / CR，其余控制字节（含 NUL、gzip magic
                        # 0x1f 0x8b）一出现就说明已经读到负载了
                        if any(b < 9 or (13 < b < 32) for b in line):
                            break
                        lines.append(line.decode("utf-8", "replace"))
                        if len(lines) >= 200:
                            break
                    self._json({"ok": True, "name": name,
                                "content": "\n".join(lines).rstrip() + "\n",
                                "editable": False, "binary": True,
                                "note": ".run 是自解压安装包：下面只有 shell 头，"
                                        "tar.gz 负载是二进制，不展示。确认内容后点「运行」安装。"})
                    return
                if os.path.getsize(p) > MAX_SCRIPT_BYTES:
                    self._json({"ok": False, "msg": "脚本过大，不支持在线预览"}, 413)
                    return
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                self._json({"ok": False, "msg": "读取失败", "detail": str(e)}, 500)
                return
            self._json({"ok": True, "name": name, "content": text,
                        # 页面只预览不编辑；保留字段给调用方判断形态
                        "editable": name.endswith(".sh")})
        elif path == "/api/script/poll":
            job = (q.get("job") or [""])[0]
            # job 会拼进 var/jobs 下的文件路径：只认 32 位十六进制，挡住 ../ 之类
            if not JOB_ID_RE.match(job):
                self._json({"ok": True, "lines": [], "done": True, "rc": None, "lost": True})
                return
            info = read_job(job)
            # total = 到目前为止的总行数；页面拿它算「新来了几行」，返回的只是最后 400 行
            self._json({"ok": True, "lines": info["lines"][-400:], "total": info.get("total", len(info["lines"])),
                        "done": info["done"], "rc": info["rc"], "lost": info["lost"]})
        else:
            self._json({"ok": False, "msg": "未知接口"}, 404)

    # ---- POST ----
    def _do_post(self):
        if hasattr(self, "_principal_cached"):
            del self._principal_cached
        path = self._norm(urllib.parse.urlsplit(self.path).path)
        self._audit_op = ""
        remote = self._remote()
        # 登录不做来源校验：它只在响应里返回会话、不设 Cookie，跨站页面读不到响应，也无法借此替人登录。
        # 手机上的 App 内置浏览器经代理访问时来源信息常常对不上，校验只会把真用户挡在外面
        if path != "/login" and not self._write_origin_ok():
            self.close_connection = True
            self._json({"ok": False, "msg": "请求来源不受信任，请从控制台操作"}, 403)
            return
        if path == "/login":
            self._login_post()
            return
        if self._locked():
            self.close_connection = True   # 请求体没读，不能复用连接
            self._json({"ok": False, "msg": "口令错误次数太多，请 5 分钟后再试"}, 429)
            return
        if not self._auth_ok():
            self.close_connection = True
            self._auth_fail_json()
            return
        if self.headers.get("X-S2-Request") != "1":
            self.close_connection = True
            self._json({"ok": False, "msg": "请求缺少安全校验头，请刷新页面"}, 403)
            return
        if path == "/api/logout":
            try:
                SESSIONS.revoke(self._session_token())
            except (OSError, ValueError):
                self._json({"ok": False, "msg": "无法保存退出状态，请检查应用数据目录后重试"}, 503)
                return
            self.close_connection = True
            self._json({"ok": True, "msg": "已退出登录"})
            return
        scope = "admin" if path.startswith("/api/notifications") else None if path == "/api/hw" or path.startswith("/api/ops/") else (
            "scripts" if path.startswith("/api/script/") else "admin" if path == "/api/restart" else "control")
        if scope and not self._require_scope(scope):
            return
        # 高危接口：没配口令时只认本机，别把 root 命令执行挂在局域网上
        if path in DANGER_PATHS and not self._danger_ok(remote):
            audit("deny:%s" % path, {"ip": remote}, remote, False)
            self.close_connection = True
            self._json({"ok": False, "msg": DANGER_DENY_MSG}, 403)
            return
        # 重启：同一道门（见 POWER_NONCES 上面的说明）
        if path in ("/api/power/prepare", "/api/power") and not self._danger_ok(remote):
            audit("deny:%s" % path, {"ip": remote}, remote, False)
            self.close_connection = True
            self._json({"ok": False, "msg": POWER_DENY_MSG}, 403)
            return
        # 上传走原始字节流，不能先当 JSON 解析；在 _body() 之前分流
        if path == "/api/script/upload":
            self._script_upload(remote)
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.close_connection = True
            self._json({"ok": False, "msg": "请求必须使用 JSON 格式"}, 415)
            return
        body, err = self._body()
        if err:
            self._json({"ok": False, "msg": "请求体过大" if err == 413 else "请求体格式错误"}, err)
            return

        if path.startswith("/api/ops/"):
            self._json({"ok": False, "msg": "接口已移除"}, 404)
            return
        if path in ("/api/notifications", "/api/notifications/test"):
            try:
                data = get_notifier().save_notifications(body) if path == "/api/notifications" else get_notifier().send_test(body)
                audit("notifications:save" if path == "/api/notifications" else "notifications:test", {}, remote, True)
                self._json({"ok": True, "data": data})
            except (Problem, OSError, ValueError) as error:
                self._json({"ok": False, "msg": str(error)}, getattr(error, "status", 400))
            return
        if path in ("/api/power/prepare", "/api/power"):
            self._power(path, body, remote)
            return

        if path == "/api/hw":
            op = body.get("op", "")
            params = body.get("params", {})
            # 电源操作只走 /api/power（确认码 + 防跨站），这个多路复用口不放行
            if op == "power":
                self._json({"ok": False, "msg": "请在「控制 → 电源」里操作"}, 400)
                return
            # 门禁是按 op 判的，不是按路径：/api/hw 是个多路复用口，
            # 里面既有只读的 status，也有能把自己关在门外的 set_port。
            if not isinstance(op, str) or not isinstance(params, dict):
                self._json({"ok": False, "msg": "操作参数格式错误"}, 400)
                return
            if op not in READ_OPS and not self._danger_ok(remote):
                audit("deny:hw:%s" % op, {"ip": remote}, remote, False)
                self._json({"ok": False, "msg": DANGER_DENY_MSG}, 403)
                return
            scope = "read" if op in READ_OPS or op == "schedule_info" else (
                "admin" if op == "set_port" else "scripts" if op == "patch" else "control")
            if not self._require_scope(scope):
                return
            self._audit_op = op
            r = hw_call(op, params)
            # 拖动滑块时一秒能来好几条手动转速：成功的只每 3 秒记一条，失败的照常全记
            quiet = op == "fan_manual" and r.get("ok") and time.time() - _AUDIT_QUIET.get(op, 0) < 3
            if not quiet:
                _AUDIT_QUIET[op] = time.time()
                audit("hw:%s" % op, params, remote, r.get("ok"))
            self._json(r)

        elif path == "/api/alerts/read":
            # ids 省略 = 全部标已读；只接受整数 id
            ids = body.get("ids")
            if ids is not None:
                if not isinstance(ids, list):
                    self._json({"ok": False, "msg": "参数格式错误"}, 400)
                    return
                ids = {i for i in ids if isinstance(i, int) and not isinstance(i, bool)}
            n = ALERTS.mark_read(ids)
            self._json({"ok": True, "msg": "", "data": {"marked": n}})

        elif path == "/api/alerts/clear":
            ALERTS.clear()
            audit("alerts:clear", {}, remote, True)
            self._json({"ok": True, "msg": "通知记录已清空"})

        elif path == "/api/script/defaults":
            self._script_defaults(remote)

        elif path == "/api/script/save":
            raw_name = str(body.get("name", "") or "").strip()
            content = body.get("content", "")
            if not raw_name:
                self._json({"ok": False, "msg": "文件名不能为空"}, 400)
                return
            # 带路径分隔符或 .. 的输入直接拒绝（不静默改名，避免用户以为存到了别处）
            if "/" in raw_name or "\\" in raw_name or ".." in raw_name:
                self._json({"ok": False, "msg": "文件名不能包含路径分隔符或 .."}, 400)
                return
            name = raw_name
            if not isinstance(content, str):
                self._json({"ok": False, "msg": "脚本内容必须是文本"}, 400)
                return
            if len(content.encode("utf-8", "ignore")) > MAX_SCRIPT_BYTES:
                self._json({"ok": False, "msg": "脚本过大（上限 %d KB）" % (MAX_SCRIPT_BYTES // 1024)}, 413)
                return
            if not name.endswith(".sh"):
                name += ".sh"
            if not SCRIPT_NAME_RE.match(name):
                self._json({"ok": False, "msg": "文件名只能包含中英文、数字、下划线、短横线与点"}, 400)
                return
            os.makedirs(SCRIPTS_DIR, exist_ok=True)
            target = os.path.join(SCRIPTS_DIR, name)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
            audit("script:save", {"name": name, "bytes": len(content)}, remote, True)
            self._json({"ok": True, "msg": "已保存 %s" % name, "scripts": hwctl.list_scripts()})

        elif path == "/api/script/delete":
            name = os.path.basename(str(body.get("name", "") or ""))
            p = script_by_name(name)
            if not p:
                self._json({"ok": False, "msg": "脚本不存在"}, 404)
                return
            # 只允许删本应用自己的脚本目录；共享目录是用户的数据盘，不碰
            if os.path.realpath(os.path.dirname(p)) != os.path.realpath(SCRIPTS_DIR):
                self._json({"ok": False, "msg": "只能删除本应用脚本目录下的文件"}, 403)
                return
            try:
                os.remove(p)
            except OSError as e:
                self._json({"ok": False, "msg": "删除失败", "detail": str(e)}, 500)
                return
            audit("script:delete", {"name": name}, remote, True)
            self._json({"ok": True, "msg": "已删除 %s" % name,
                        "scripts": hwctl.list_scripts()})

        elif path == "/api/script/start":
            name = os.path.basename(str(body.get("name", "") or ""))
            factory = body.get("factory") is True
            p = factory_script(name) if factory else script_by_name(name)
            if not p:
                self._json({"ok": False, "msg": "脚本不存在"})
                return
            try:
                with SCRIPT_START_LOCK:
                    inspected = inspect_script(name, factory)
                    if body.get("sha256") != inspected["sha256"]:
                        raise Problem("请先审查脚本内容并确认摘要", 409)
                    if inspected["risks"] and (body.get("risk_confirm") != "yes" or body.get("risk_ack") != inspected["sha256"]):
                        raise Problem("此脚本包含系统改动，请查看风险并输入 yes 确认当前版本", 409)
                    active_jobs = [j for j in managed_jobs() if not j["done"]]
                    if len(active_jobs) >= MAX_JOBS:
                        raise Problem("正在运行的任务已达到上限", 409)
                    if any(j["name"] == name for j in active_jobs):
                        raise Problem("该脚本正在运行，不能重复启动", 409)
                    timeout = integer(body.get("timeout", 600), 10, 3600)
                    job = start_job(p, timeout=timeout, expected_hash=inspected["sha256"])
                    script_review(("factory:" if factory else "") + name, {"hash": inspected["sha256"], "content": inspected["content"]})
                audit("script:start", {"name": name, "sha256": inspected["sha256"]}, remote, True)
                self._json({"ok": True, "job": job, "name": name})
            except Problem as error:
                self._json({"ok": False, "msg": str(error)}, error.status)

        elif path == "/api/script/cancel":
            identifier = body.get("id", "")
            job = next((j for j in managed_jobs() if j["id"] == identifier), None)
            if not job or job["done"]:
                self._json({"ok": False, "msg": "任务已结束或不存在"}, 409)
                return
            if os.name != "posix":
                self._json({"ok": False, "msg": "任务组取消仅支持 Linux 设备"}, 400)
                return
            try:
                with open(_job_files(identifier)[2]) as stream:
                    pid = int(stream.read().strip())
                # A persisted PID may have been reused; verify it still supervises this exact job.
                with open("/proc/%d/cmdline" % pid, "rb") as stream:
                    command = stream.read()
                if b"job_runner.py" not in command or identifier.encode() not in command:
                    raise ValueError("任务进程已变化")
                os.kill(pid, signal.SIGTERM)
                self._json({"ok": True, "msg": "已请求取消任务"})
            except (OSError, ValueError):
                self._json({"ok": False, "msg": "无法确认任务进程，未发送终止信号"}, 409)

        elif path == "/api/restart":
            port = body.get("port")
            msg = ""
            if port:
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    self._json({"ok": False, "msg": "端口非法"}, 400)
                    return
                r = hw_call("set_port", {"port": port})
                msg = r.get("msg", "")
                if not r.get("ok"):
                    self._json(r)
                    return
            cmd = os.environ.get("S2_APP_CMD", "/var/apps/fnOS-S2/cmd/main")
            # 路径走 $0，不拼进脚本正文 —— 原来用 % 把 cmd 插进 bash -c 的字符串里，
            # 只要那个环境变量里有分号就是一条命令注入。这里 cmd 变成 bash 的
            # 第 0 个参数，无论内容是什么都只被当成一个可执行文件路径。
            subprocess.Popen(
                ["bash", "-c",
                 'sleep 1; "$0" stop >/dev/null 2>&1; sleep 2; "$0" start >/dev/null 2>&1',
                 cmd],
                stdin=subprocess.DEVNULL, start_new_session=True)
            audit("app:restart", {"port": port}, remote, True)
            self._json({"ok": True, "msg": msg or "应用正在重启"})

        else:
            self._json({"ok": False, "msg": "未知接口"}, 404)


class QuietServer(ThreadingHTTPServer):
    """浏览器关页面、手机切后台、代理超时都会把连接中途掐掉。这不是故障，
    默认的 handle_error 却会往 server.log 打一整段堆栈，把真正的错误淹掉。只对断连静音，其他错误照常记录。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    global _ALERT_WATCHER
    write_pid()
    cleanup_jobs()
    get_notifier().start()
    if os.environ.get("S2_ALERTS", "1") != "0":
        _ALERT_WATCHER = alerts.Watcher(ALERTS, _alert_collect)
        _ALERT_WATCHER.start()
    srv = QuietServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write("S2 硬件控制台 监听 0.0.0.0:%d (root=%s)\n" % (PORT, hwctl.is_root()))
    if not TOKEN:
        # 明确写出来，别让人以为「装了就有口令」。这条警告对应 DANGER_PATHS 的门禁策略。
        sys.stderr.write(
            "[安全提示] 未配置访问口令 TOKEN：脚本上传/执行、改端口、重启仅允许本机调用；\n"
            "           如需在局域网内使用这些功能，请到「应用设置 → 配置」设置口令并保存，\n"
            "           应用重启后打开 http://<设备地址>:%d/ 输入口令即可。\n" % PORT)
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
