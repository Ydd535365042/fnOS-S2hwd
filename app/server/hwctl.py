#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hwctl —— S2 硬件控制台的硬件控制层

设计要点
--------
1) 所有操作统一抽象为 {op, params}，由 OPS 表分发。
   这样「root 直接执行」与「本地 agent 代理执行」两条路径完全同构：
   非 root 时把同样的 op 转发给以 root 常驻的 s2hw-agent 即可。
2) 配置写入采用「保留注释的 KEY=VALUE 就地替换」，不破坏原 conf 文件的说明文字。
3) 任何写操作都不会直接改动系统自带脚本，只改 conf + 重启对应服务，
   保证卸载/回滚后系统仍可自洽。
4) 安全：所有进入 systemd / 文件路径的服务名与 LED 名都经正则白名单校验；
   能不经过 shell 的调用一律用 argv 形式（subprocess 列表），杜绝命令注入。
5) 路径可通过环境变量覆盖，便于测试与非常规部署。
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

# hwinfo 是同目录的兄弟模块。server.py 和 s2hw-agent.py 在 import hwctl 之前都会
# 把 server 目录插进 sys.path，所以多数情况下普通 import 就够用；但测试里有的
# 调用方（_tests/smoke_hwctl.py）用 importlib.util.spec_from_file_location 按
# 绝对路径加载 hwctl.py，并不会把 hwctl.py 所在目录自动加进 sys.path——那种
# 加载方式下 "import hwinfo" 会报 ModuleNotFoundError。这里自己把本文件所在目录
# 插进 sys.path 兜底，不依赖调用方有没有做这一步。
import sys as _sys
_HWCTL_DIR = os.path.dirname(os.path.abspath(__file__))
if _HWCTL_DIR not in _sys.path:
    _sys.path.insert(0, _HWCTL_DIR)
import hwinfo  # noqa: E402
import s2security
import s2hardware
import s2native
import s2dtb

# ---------------- 应用版本 ----------------
# 装到设备上以后，应用中心会把真实版本写进 <应用根>/manifest 的 version= 字段，
# 那个才是权威值；开发机 / 直接跑源码时没有这个文件，就退回下面的常量。
# 前端设置页显示的是这里返回的值 —— 不要再往 HTML 里写死版本号（以前写死过一个
# v1.5.1，升到 1.6.0 之后页面上还挂着旧的）。
APP_VER = "1.0.3"
APP_MANIFEST_NAME = "manifest"


def app_version():
    """优先读应用中心写的 manifest，读不到就用内置常量。"""
    dests = [os.environ.get("TRIM_APPDEST", ""), "/var/apps/fnOS-S2"]
    for d in dests:
        if not d:
            continue
        path = os.path.join(d, APP_MANIFEST_NAME)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    key, sep, val = line.partition("=")
                    if sep and key.strip() == "version" and val.strip():
                        return val.strip()
        except OSError:
            continue
    return APP_VER


# 板型不是「演示数据」，是这个产品的固定身份；但没有理由把它写死在 HTML 里 ——
# 同一套固件换块板子跑，页面就会报一个错的型号，而且没人会想起来去改。
BOARD_FALLBACK = "fnOS-S2 (RK3568)"


def board_model():
    """优先读设备树 model，读不到退回产品名。

    设备树里是 NUL 结尾的裸字符串，sysfs_read 的 strip() 只处理空白，
    不处理 \\0，所以这里要自己剥一次，否则界面上会多出一个方块字符。
    """
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        val = sysfs_read(path, "").strip("\x00").strip()
        if val:
            return val
    return BOARD_FALLBACK


# ---------------- 路径 ----------------
ETC_SATA_LED = os.environ.get("S2_ETC_SATA_LED", "/etc/sata-led.conf")
APP_CONF_DIR = os.environ.get("S2_APP_CONF_DIR", "/etc/s2hwctl")
NATIVE = s2native.Native()
NATIVE_ENABLED = NATIVE.capabilities()["ready"] or s2native.read(os.path.join(APP_CONF_DIR, "backend")) == "native"
ETC_FAN = os.environ.get("S2_ETC_FAN", os.path.join(APP_CONF_DIR, "fan.conf") if NATIVE_ENABLED else "/etc/fan-control.conf")
APP_BEEP_CONF = os.path.join(APP_CONF_DIR, "beep.conf")
LED_OVERRIDE_CONF = os.path.join(APP_CONF_DIR, "led-override.conf")
APP_SCHED_CONF = os.environ.get("S2_SCHED_CONF", "/etc/s2-power-schedule.conf")

VAR_DIR = os.environ.get("S2_VAR_DIR", "/var/apps/fnOS-S2/var")
SCRIPTS_DIR = os.path.join(VAR_DIR, "scripts")
SHARE_SCRIPTS = os.environ.get("TRIM_DATA_SHARE_PATHS", "").split(":")[0]

FAN_RUN_DIR = os.environ.get("S2_FAN_RUN_DIR", "/run/fnos-s2-native" if NATIVE_ENABLED else "/var/lib/fan-control")
FAN_STATUS = os.path.join(FAN_RUN_DIR, "status")

LED_DIR = os.environ.get("S2_LED_DIR", "/sys/class/leds")
BUZZER_SYSFS = os.environ.get("S2_BUZZER_SYSFS", "/sys/devices/platform/gpio_ctrl/buzzer_gpio")
GPIO_BEEPER = 18
SCHED_APPLY_BIN = os.environ.get("S2_SCHED_APPLY_BIN", "/usr/local/sbin/s2-power-schedule.sh")
POWER_LED_STATUS_BIN = os.environ.get("S2_POWER_LED_STATUS_BIN",
                                      "/usr/local/sbin/power-led-net.sh")

HW_SERVICES = [
    "fan-control", "sata-led-manager", "power-led-net",
    "power-key-daemon", "random-mac", "set-hostname",
    "selfheal", "boot-version-guard",
    "s2-power-schedule",
]
if NATIVE_ENABLED:
    HW_SERVICES = [s2native.SERVICE, "s2-power-schedule"]

# 服务名 / LED 名白名单：只允许 systemd 与 sysfs 合法字符
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
SERVICE_ACTIONS = ("start", "stop", "restart", "reload", "enable", "disable",
                   "reset-failed", "try-restart")

# ---------------- 补丁模块（可插拔，运行时发现） ----------------
# 每个模块一个子目录：<PATCH_DIR>/<id>/module.conf（KEY=VALUE）+ 随附脚本。
# module.conf 字段：
#   ID / TITLE / CODE / DESC / SCRIPT / CONF / SERVICES / ENABLE / DISABLE
#   MODULE_VER / REQUIRES
PATCH_DIR = os.environ.get("S2_PATCH_DIR", "/etc/s2hwctl/patches.d")
# 模块接口框架版本；module.conf 的 REQUIRES 与之比较，高于本值则跳过该模块
FRAMEWORK_VER = "1.0.0"
# 最近一次扫描发现的问题，随 collect_status 暴露给前端
PATCH_SCAN_WARNINGS = []

MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MODULE_CODE_RE = re.compile(r"^[A-Z0-9]{1,8}$")
SEMVER_RE = re.compile(r"^\d+(\.\d+){0,2}$")
# 模块声明的服务名：systemd unit 名，禁止任何 shell 元字符
MODULE_UNIT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.@\-]{0,127}"
    r"(\.(service|timer|socket|path|target|mount))?$")
# 模块声明的脚本子命令：单段标识符，禁止空格 / 分号 / $() / 反引号 / 斜杠
MODULE_SUBCMD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")


# ---------------- 基础工具 ----------------
# 只保留 run() 的 argv 形式。原先还有一个 shell=True 的 sh()，全项目零调用，
# 留着就是给后来的人一把「顺手拼字符串」的枪，已删除。
def run(argv, timeout=20):
    """不经 shell 执行 argv 列表，返回 (rc, output)。杜绝命令注入。

    output 在失败路径上是英文（"timeout" / "not found: x"）——这是内部排障用的
    原始文本，不是说给用户看的话。调用方若要把结果直接变成 /api/hw 的 msg 展示给
    用户，必须走 msg_detail() 包一层中文句子，别把这里的英文原样透给前端
    （HW_SPEC §4：提示语用中文，原文放 detail）。
    """
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "not found: %s" % argv[0]
    except Exception as e:  # noqa
        return 1, str(e)


# ---------------- 中文提示 + 原文 detail ----------------
# HW_SPEC §4：/api/hw 的 msg 必须是中文句子，systemctl/smartctl/Python 异常等原始
# 英文文本放进 detail 字段（前端 console.error，不直接展示）。dispatch() 对外的
# 契约是 (ok, data)，data 可以是字符串（纯消息）或字典（数据负载）——这里新增的
# "既有中文消息又想带原文 detail" 场景，用一个带私有标记键的 dict 表示，
# hw_call()（server.py）识别到这个标记就拆成 msg + detail 两个顶层字段，
# 不识别时（旧版 server.py / s2hw-agent.py 的其它调用方）也只是把它当成普通的
# data 字典返回，不会报错，只是 detail 多展示了一层，不影响功能。
_MSG_DETAIL_MARK = "__s2_msg_detail__"


def msg_detail(zh, detail=""):
    """包一条「中文消息 + 原始详情」。detail 为空时就是普通中文消息。"""
    return {_MSG_DETAIL_MARK: True, "msg": zh, "detail": detail or ""}


def is_msg_detail(data):
    return isinstance(data, dict) and data.get(_MSG_DETAIL_MARK) is True


def current_uid():
    """当前有效 UID；非 POSIX 平台（如开发机 Windows）返回 -1。"""
    fn = getattr(os, "geteuid", None)
    return fn() if fn else -1


def is_root():
    return current_uid() == 0


def safe_name(name):
    """校验服务名 / LED 名是否合法，不合法返回 None。"""
    if not isinstance(name, str):
        return None
    return name if SAFE_NAME_RE.match(name) else None


# read_kv() / load_app_conf() 的 mtime 缓存：这些 conf 文件（fan-control 的
# status、sata-led.conf、led-override.conf、beep.conf…）在 10 秒一次的
# /api/sensor、/api/state 轮询里每次都要重新 open+parse，但绝大多数时间它们
# 根本没变过。按 (path, mtime) 做缓存——mtime 没变就直接返回上次解析好的 dict
# 的浅拷贝（浅拷贝是因为调用方可能会往返回值上 .update()，不能共享同一个对象）。
# 文件不存在时 mtime 取不到，每次都会重新「尝试」一次 stat，开销可忽略
# （find 不到文件的 stat 本身就是几微秒级的系统调用，不值得单独缓存「不存在」）。
_KV_CACHE = {}


def read_kv(path):
    """解析 KEY=VALUE（忽略 # 注释），按文件 mtime 缓存。"""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _KV_CACHE.pop(path, None)
        return {}
    hit = _KV_CACHE.get(path)
    if hit and hit[0] == mtime:
        return dict(hit[1])
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    except (FileNotFoundError, OSError):
        pass
    _KV_CACHE[path] = (mtime, dict(out))
    return out


def write_kv(path, updates, header=None):
    """就地替换 KEY=VALUE，保留注释与未涉及的键；文件不存在则新建。
    先写临时文件再 os.replace，保证原子性。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    seen = set()
    new_lines = []
    for line in lines:
        s = line.strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", s)
        if m and m.group(1) in updates:
            k = m.group(1)
            new_lines.append("%s=%s" % (k, updates[k]))
            seen.add(k)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append("%s=%s" % (k, v))
    if header and not lines:
        new_lines.insert(0, header)
    s2security.atomic_text(path, "\n".join(new_lines) + "\n",
                           mode=0o600 if os.path.basename(path) == "settings.conf" else None)
    _KV_CACHE.pop(path, None)  # 自己刚写的文件，别让 mtime 缓存把旧内容吐回去
    return True


def atomic_write(path, text, mode=None):
    """原子写文本文件，可选设置权限。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(text))
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def sysfs_write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except Exception:
        return False


def sysfs_read(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default


def _classify(active_state, unit_type, result):
    """把 systemd 的状态三件套翻译成前端用的四个值。

      active  -> 常驻服务正在运行（绿）
      done    -> 一次性(oneshot)服务已成功执行并退出（绿/中性，非故障）
      unknown -> 服务单元不存在（前端隐藏）
      其它    -> 真正的异常（红）
    关键点：boot-version-guard / s2-power-schedule 等 oneshot 服务开机跑一次就退出，
    systemd 报 inactive 是其正常终态，不能当作“未运行”标红。
    """
    st = (active_state or "unknown").strip() or "unknown"
    if st == "active":
        return "active"
    oneshot = (unit_type or "").strip() == "oneshot"
    # 一次性服务跑完就 inactive，这是正常终态，不是「没运行」。无论 Result 是 success 还是
    # exit-code（关机 / deploy.sh 的 restart 打断 / 手动 stop，在 KillMode=none、
    # TimeoutStopFailureMode 下常记成 exit-code），都当「已执行」。真失败是 failed 状态。
    # activating / deactivating 仍在启停过程中，原样透传，不提前判成已执行。
    if oneshot and st == "inactive":
        return "done"
    return st


# services_state() 缓存：15 秒，/api/sensor 与 /api/state 共用同一份。
# key 是请求的服务名元组（不同调用方传的列表不完全相同：collect_sensor() 只查
# HW_SERVICES，collect_status() 还要加上补丁模块声明的服务）——不同 key 各自
# 缓存，互不影响；任何写操作改变了服务状态后调 bust_services_cache() 立即失效，
# 保证「写了就能马上看到新状态」而不是等 15 秒。
_SVC_CACHE = {}
_SVC_CACHE_LOCK = threading.Lock()
_SVC_CACHE_TTL = 15


def bust_services_cache():
    """服务状态被写操作改变后调用：清空缓存，下一次查询强制重新 systemctl show。"""
    with _SVC_CACHE_LOCK:
        _SVC_CACHE.clear()


def _norm_unit(n):
    """systemctl 回显的 Id 带 .service，前端用裸名：统一剥掉 .service（.timer 等保留）。"""
    return n[:-8] if n.endswith(".service") else n


def parse_systemctl_show(out):
    """解析多单元的 `systemctl show -p ...` 输出 → {裸名: {属性: 值}}。

    单元之间用空行分隔，但段内属性的顺序不是 -p 里写的顺序：systemctl 先打印
    服务专有属性（Type、Result），后打印通用属性（Id、ActiveState）。以前读到 Id
    才开始记录，排在前面的 Type 全被丢掉 —— oneshot 永远认不出来，「启动版本保护」
    这种跑完就退出的服务一直显示红色「未运行」。所以整段收齐再按 Id 归档。
    """
    found = {}

    def flush(block):
        uid = block.get("Id")
        if uid:
            found[_norm_unit(uid)] = {k: block.get(k, "") for k in ("LoadState", "ActiveState", "Type", "Result")}

    block = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            flush(block)
            block = {}
            continue
        k, sep, v = line.partition("=")
        if sep:                       # 忽略 "Unit x.service could not be found." 之类
            block[k] = v
    flush(block)
    return found


def services_state(names):
    """一次 systemctl show 取回全部服务的状态，结果缓存 15 秒。

    原先每个服务都要 1–2 次 systemctl 调用（is-active；非 active 时再 show
    取 Type/Result 判断是不是 oneshot），9 个服务就是 14 次进程 —— 而
    /api/state 是 10 秒一次的轮询，在 RK3568 上每次要烧掉近 1 秒 CPU，
    还让设备一直无法真正空闲下来。这里合并成一次调用，再加 15 秒缓存：
    /api/sensor（10 秒轮询）大多数情况下直接命中缓存，0 次进程。

    返回 {名字: 状态串}；systemctl 不存在（开发机）时全部为 unknown（这个结果
    也会被缓存 15 秒，避免开发机上每次轮询都现查一次 127）。
    """
    names = list(names)
    if not names:
        return {}
    key = tuple(sorted(names))
    now = time.time()
    with _SVC_CACHE_LOCK:
        hit = _SVC_CACHE.get(key)
        if hit and now - hit[0] < _SVC_CACHE_TTL:
            return dict(hit[1])
    # 显式补 .service：systemctl 对裸名会自己补后缀，但那是它内部的行为，
    # 不同版本对 show 的处理不完全一致，我们自己写死更稳。
    units = [n if "." in n else n + ".service" for n in names]
    rc, out = run(["systemctl", "show", "-p", "Id,LoadState,ActiveState,Type,Result"] + units, timeout=15)
    if rc == 127:                      # 没有 systemctl（非 systemd 环境 / 开发机）
        res = {n: "unknown" for n in names}
        with _SVC_CACHE_LOCK:
            _SVC_CACHE[key] = (now, dict(res))
        return res

    # 归一化：裸名和带 .service 的名字映射到同一个 key（见 _norm_unit）
    found = parse_systemctl_show(out)

    res = {}
    for n in names:
        # 之前这里用原始 n 查表，模块若把服务名写成 "foo.service" 就恒查不到（key 是 "foo"），
        # 状态永远显示「未知」。用归一化后的名字回查即可。
        info = found.get(_norm_unit(n))
        # 没装的单元 systemctl 也会回一段（LoadState=not-found、ActiveState=inactive），
        # 不能当成「未运行」标红，按「查不到」处理（前端灰点）
        if not info or info.get("LoadState") == "not-found":
            res[n] = "unknown"
            continue
        res[n] = _classify(info["ActiveState"], info["Type"], info["Result"])
    with _SVC_CACHE_LOCK:
        _SVC_CACHE[key] = (now, dict(res))
    return res


def service_active(name):
    """单个服务的状态（保留旧接口；内部走批量实现，避免两套判定逻辑分叉）。"""
    if not safe_name(name):
        return "unknown"
    return services_state([name]).get(name, "unknown")


def restart_service(name):
    """重启服务；服务名必须过白名单。"""
    if not safe_name(name):
        return False, "非法服务名：%s" % name
    rc, out = run(["systemctl", "restart", name], timeout=40)
    bust_services_cache()  # 状态刚被改变，清缓存，前端下一次轮询立即看到新状态
    if rc != 0 and "not found" in out.lower():
        return False, "服务 %s 不存在" % name
    return True, out.strip() or "已重启"


# ---------------- LED ----------------
def list_leds():
    leds = []
    if not os.path.isdir(LED_DIR):
        return leds
    for name in sorted(os.listdir(LED_DIR)):
        base = os.path.join(LED_DIR, name)
        triggers = sysfs_read(os.path.join(base, "trigger"), "")
        leds.append({
            "name": name,
            "brightness": sysfs_read(os.path.join(base, "brightness"), "0"),
            "max_brightness": sysfs_read(os.path.join(base, "max_brightness"), "255"),
            "trigger": (re.findall(r"\[(.*?)\]", triggers) or ["none"])[0],
        })
    return leds


def apply_led(name, mode="on", brightness=None, on_ms=500, off_ms=500):
    """
    保留供「脚本修复」等后续扩展使用；电源灯已由 power-led-net 常驻接管。
    mode: on | off | blink
    返回 (ok, msg)
    """
    if not safe_name(name):
        return False, "非法 LED 名：%s" % name
    base = os.path.join(LED_DIR, name)
    if not os.path.isdir(base):
        return False, "LED %s 不存在" % name
    maxb = sysfs_read(os.path.join(base, "max_brightness"), "255")
    try:
        maxb = int(maxb) or 255
    except ValueError:
        maxb = 255
    if mode == "blink":
        sysfs_write(os.path.join(base, "trigger"), "timer")
        sysfs_write(os.path.join(base, "delay_on"), str(int(on_ms)))
        sysfs_write(os.path.join(base, "delay_off"), str(int(off_ms)))
        return True, "已设置为闪烁 %sms/%sms" % (on_ms, off_ms)
    sysfs_write(os.path.join(base, "trigger"), "none")
    val = maxb if mode == "on" else 0
    if brightness is not None and mode == "on":
        val = max(0, min(maxb, int(brightness)))
    sysfs_write(os.path.join(base, "brightness"), str(val))
    return True, "已%s" % ("点亮" if val else "熄灭")


_APP_CONF_CACHE = {}


def load_app_conf(path):
    """同 read_kv()：按文件 mtime 缓存，避免每次轮询都重新 open+json.load。"""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _APP_CONF_CACHE.pop(path, None)
        return {}
    hit = _APP_CONF_CACHE.get(path)
    if hit and hit[0] == mtime:
        return dict(hit[1])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = data if isinstance(data, dict) else {}
    except Exception:
        data = {}
    _APP_CONF_CACHE[path] = (mtime, dict(data))
    return data


def save_app_conf(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    _APP_CONF_CACHE.pop(path, None)  # 自己刚写的文件，别让 mtime 缓存把旧内容吐回去
    return True


# ---------------- 定时开关机 ----------------
def rtc_wake_supported():
    """检测 RTC 是否支持定时唤醒（wakealarm 可写即认为支持）"""
    for p in glob.glob("/sys/class/rtc/rtc*/wakealarm"):
        if os.access(p, os.W_OK):
            return True
    return False


def op_set_power_schedule(params):
    return s2hardware.dispatch(_sys.modules[__name__], "set_power_schedule", params)


# ---------------- 立即重启 ----------------
# 只由 server.py 的 /api/power 调用（那里有门禁 + 一次性确认码 + 审计）；/api/hw 不放行 "power"。
# 网页上不提供关机：关了之后网页就够不着这台设备了，只能靠人去按电源键。
# 定时开关机仍由 s2-power-schedule 负责（概览「定时开关机」）。
POWER_ACTIONS = ("reboot",)
POWER_DELAY = 1.5        # 先把 HTTP 响应发出去再执行，否则前端只看到「连接中断」


def _power_exec(action):
    """真正执行。systemctl --no-block 只把任务排进 systemd 就返回；不可用时退到 /sbin。"""
    sc = shutil.which("systemctl")
    if sc:
        rc, _ = run([sc, "--no-block", "reboot"], timeout=10)
        if rc == 0:
            return
    for p in ("/sbin/reboot", "/usr/sbin/reboot"):
        if os.path.exists(p):
            run([p], timeout=10)
            return


def op_power(params):
    action = str(params.get("action") or "")
    if action not in POWER_ACTIONS:
        return False, "未知的电源操作"
    if not os.path.isdir("/sys/class") or os.name != "posix":
        return False, "只能在设备上执行"
    t = threading.Timer(POWER_DELAY, _power_exec, args=(action,))
    t.daemon = False
    t.start()
    return True, "设备正在重启，大约 1–2 分钟后恢复"


# ---------------- 蜂鸣器 ----------------
def beep(ms=280):
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = 280
    ms = max(30, min(5000, ms))
    if NATIVE_ENABLED:
        return NATIVE.beep(ms)
    if not legacy_controller_available():
        return False, "标准蜂鸣器尚未就绪，请先核对设备树；未尝试直写 GPIO"
    sec = ms / 1000.0
    if os.path.exists(BUZZER_SYSFS) and os.access(BUZZER_SYSFS, os.W_OK):
        if not sysfs_write(BUZZER_SYSFS, "buzzer_en:2"):
            return False, "蜂鸣器写入失败"
        time.sleep(sec)
        sysfs_write(BUZZER_SYSFS, "buzzer_en:0")
        return True, "已鸣叫 %d 毫秒" % ms
    g = "/sys/class/gpio/gpio%d" % GPIO_BEEPER
    if not os.path.exists(g):
        sysfs_write("/sys/class/gpio/export", GPIO_BEEPER)
        time.sleep(0.1)
    if os.path.exists(os.path.join(g, "direction")):
        sysfs_write(os.path.join(g, "direction"), "out")
        sysfs_write(os.path.join(g, "value"), "1")
        time.sleep(sec)
        sysfs_write(os.path.join(g, "value"), "0")
        sysfs_write("/sys/class/gpio/unexport", GPIO_BEEPER)
        return True, "已鸣叫 %d 毫秒" % ms
    return False, msg_detail("没有找到蜂鸣器", "gpio_ctrl / GPIO%d 均不可用" % GPIO_BEEPER)


# ---------------- 系统资源采集（内存 / CPU% / 负载 / 网络速率 / 磁盘） ----------------
# 这些读的全是 /proc、/sys 下的标准文件，不经 shell，开发机(Windows)上读不到就返回空，
# 前端拿到空值显示「--」，不会报错。真机(RK3568/Linux)上才有真实数据。
_CPU_PREV = {"t": 0.0, "idle": 0, "total": 0}
_NET_PREV = {"t": 0.0, "rx": 0, "tx": 0}
_NET_IGNORE = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap")
# /proc/diskstats 每设备一行："major minor name <14个统计字段…>"。
# 按整行 split() 后 0-based 下标：
#   parts[5]  = 读出的扇区数（字段3：sectors read）
#   parts[9]  = 写入的扇区数（字段7：sectors written）
#   parts[12] = 设备忙的累计时间，单位 ms（字段10：time doing I/Os / io_ticks）
# 参见内核文档 Documentation/admin-guide/iostats.rst。扇区固定 512 字节。
_DISK_PREV = {}


def read_meminfo():
    """/proc/meminfo → {total_kb, avail_kb, used_kb, used_pct}。读不到返回空 dict。"""
    kv = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                k, _, v = line.partition(":")
                kv[k.strip()] = v.strip()
    except OSError:
        return {}
    def _kb(key):
        try:
            return int(kv.get(key, "0").split()[0])
        except (ValueError, IndexError):
            return 0
    total = _kb("MemTotal")
    if not total:
        return {}
    # MemAvailable 是内核算好的「可立即使用」值，比 free+buffers 更准
    avail = _kb("MemAvailable") or (_kb("MemFree") + _kb("Buffers") + _kb("Cached"))
    used = max(0, total - avail)
    return {"total_kb": total, "avail_kb": avail, "used_kb": used,
            "used_pct": round(used * 100.0 / total, 1)}


def read_cpu_pct():
    """两次采样之间的 CPU 占用率 %。首次调用无基线，返回 None。"""
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        nums = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)
    d_total = total - _CPU_PREV["total"]
    d_idle = idle - _CPU_PREV["idle"]
    _CPU_PREV["total"], _CPU_PREV["idle"] = total, idle
    if d_total <= 0:
        return None
    return round((1 - d_idle / d_total) * 100.0, 1)


def read_loadavg():
    try:
        with open("/proc/loadavg", "r") as f:
            return f.read().split()[0]
    except (OSError, IndexError):
        return ""


def primary_iface():
    """选第一个不在 _NET_IGNORE 里、operstate 为 up 的网卡；都没有则空串。"""
    base = "/sys/class/net"
    if not os.path.isdir(base):
        return ""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ""
    for name in names:
        if any(name.startswith(p) for p in _NET_IGNORE):
            continue
        state = sysfs_read(os.path.join(base, name, "operstate"), "")
        if state == "up":
            return name
    return ""


def read_net_rate():
    """两次采样之间的网络收发速率（字节/秒）+ 当前主用网卡名。首次调用无基线，速率为空。"""
    now = time.time()
    rx = tx = 0
    try:
        with open("/proc/net/dev", "r") as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                name = name.strip()
                if not rest or any(name.startswith(p) for p in _NET_IGNORE):
                    continue
                cols = rest.split()
                if len(cols) >= 9:
                    rx += int(cols[0]); tx += int(cols[8])
    except (OSError, ValueError, IndexError):
        return {"rx_bps": 0, "tx_bps": 0, "iface": primary_iface()}
    dt = now - _NET_PREV["t"]
    prev_rx, prev_tx = _NET_PREV["rx"], _NET_PREV["tx"]
    _NET_PREV.update({"t": now, "rx": rx, "tx": tx})
    iface = primary_iface()
    if dt <= 0 or prev_rx == 0:
        return {"rx_bps": 0, "tx_bps": 0, "iface": iface}
    return {"rx_bps": max(0, int((rx - prev_rx) / dt)),
            "tx_bps": max(0, int((tx - prev_tx) / dt)),
            "iface": iface}


def read_diskstats():
    """/proc/diskstats → {设备名: (sectors_read, sectors_written, io_ticks_ms)}。
    读不到 / 行不完整的设备直接跳过，不抛异常。"""
    out = {}
    try:
        with open("/proc/diskstats", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 13:
            continue
        name = parts[2]
        try:
            rd_sectors = int(parts[5])
            wr_sectors = int(parts[9])
            io_ticks = int(parts[12])
        except ValueError:
            continue
        out[name] = (rd_sectors, wr_sectors, io_ticks)
    return out


def _disk_rate(name, rd_sectors, wr_sectors, io_ticks, now):
    """两次采样之间某块设备的 读/写速率(字节/秒) 与 忙碌占比(%)。
    首次调用（无基线）返回全 0，与 read_cpu_pct/read_net_rate 的「首次无基线」约定一致。"""
    prev = _DISK_PREV.get(name)
    _DISK_PREV[name] = (now, rd_sectors, wr_sectors, io_ticks)
    if not prev:
        return 0, 0, 0.0
    prev_t, prev_rd, prev_wr, prev_ticks = prev
    dt = now - prev_t
    if dt <= 0:
        return 0, 0, 0.0
    rd_bps = max(0, int((rd_sectors - prev_rd) * 512 / dt))
    wr_bps = max(0, int((wr_sectors - prev_wr) * 512 / dt))
    # io_ticks 是设备「忙」的累计毫秒数，两次采样之差 / 经过的毫秒数 = 忙碌占比
    d_ticks = io_ticks - prev_ticks
    busy_pct = max(0.0, min(100.0, d_ticks / (dt * 1000.0) * 100.0)) if dt > 0 else 0.0
    return rd_bps, wr_bps, round(busy_pct, 1)


def list_disks():
    """块设备清单（容量 / 型号 / 温度 / 读写速率 / 忙碌占比）。
    只列整盘(sd*, nvme*n*, mmcblk*)，不列分区。
    温度只读取风扇服务的缓存；不访问 drivetemp 或主动读盘。
    速率/忙碌占比来自 /proc/diskstats 两次采样之差，首次调用无基线返回 0。"""
    out = []
    base = "/sys/block"
    if not os.path.isdir(base):
        return out
    hdd_temps = {}
    fs = read_kv(FAN_STATUS) if os.path.exists(FAN_STATUS) else {}
    try:
        if time.time() - os.path.getmtime(FAN_STATUS) > 90:
            fs = {}
    except OSError:
        fs = {}
    for k, v in fs.items():
        if k.startswith("temp_") and v:
            hdd_temps[k[5:]] = v
    # Current fan firmware reports a single snapshot; non-numeric states are not temperatures.
    for item in fs.get("disks", "").split():
        name, _, value = item.partition("=")
        if value.isdigit():
            hdd_temps[name] = value
    stats = read_diskstats()
    now = time.time()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if not re.match(r"^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+)$", name):
            continue
        d = os.path.join(base, name)
        try:
            sectors = int(sysfs_read(os.path.join(d, "size"), "0") or "0")
        except ValueError:
            sectors = 0
        model = sysfs_read(os.path.join(d, "device", "model"), "") or \
            sysfs_read(os.path.join(d, "device", "name"), "")
        rot = sysfs_read(os.path.join(d, "queue", "rotational"), "1")
        temp = hdd_temps.get(name, "")
        rd_bps = wr_bps = 0
        busy_pct = 0.0
        if name in stats:
            rd_sectors, wr_sectors, io_ticks = stats[name]
            rd_bps, wr_bps, busy_pct = _disk_rate(name, rd_sectors, wr_sectors, io_ticks, now)
        out.append({
            "name": name,
            "bytes": sectors * 512,
            "model": model.strip(),
            "rotational": rot == "1",
            "temp": temp,
            "busy_pct": busy_pct,
            "rd_bps": rd_bps,
            "wr_bps": wr_bps,
        })
    return out


def _uptime_fallback():
    """没有 /proc/uptime 时（Windows 开发机本地联调）取开机毫秒数，返回与 split() 同形的列表。
    设备上永远走 /proc/uptime，这里不会被调用。"""
    if os.name != "nt":
        return []
    try:
        import ctypes
        f = ctypes.windll.kernel32.GetTickCount64
        f.restype = ctypes.c_ulonglong
        return ["%.2f" % (f() / 1000.0)]
    except Exception:
        return []


def _meminfo_fallback():
    """没有 /proc/meminfo 时（Windows 开发机本地联调）用 GlobalMemoryStatusEx 取内存，
    字段与 read_meminfo() 同形。设备上永远走 /proc/meminfo，这里不会被调用。"""
    if os.name != "nt":
        return {}
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return {}
        total, avail = m.ullTotalPhys // 1024, m.ullAvailPhys // 1024
        if not total:
            return {}
        used = max(0, total - avail)
        return {"total_kb": total, "avail_kb": avail, "used_kb": used,
                "used_pct": round(used * 100.0 / total, 1)}
    except Exception:
        return {}


# ---------------- 状态汇总 ----------------
def _fan_fields(fan_status):
    """把 fan-control 写的 status 文件翻成前端字段。

    v1.6 起固件是全温控曲线，没有「挡位」概念：
      mode      = auto（写死）
      level     = 实际占空比 %（不是挡位，固件里 level=$duty）
      duty_pct  = 同上，保留双字段以兼容老前端
    """
    return {
        "cpu_temp": fan_status.get("cpu_temp", ""),
        "hdd_max_temp": fan_status.get("hdd_max_temp", ""),
        "fan_level": fan_status.get("level", ""),
        "fan_duty": fan_status.get("duty_pct", "") or fan_status.get("level", ""),
        "fan_mode": fan_status.get("mode", ""),
        "fan_source": fan_status.get("hdd_source", ""),
        "disks": fan_status.get("disks", ""),
    }


def fan_snapshot():
    status = read_kv(FAN_STATUS) if os.path.exists(FAN_STATUS) else {}
    if NATIVE_ENABLED:
        node = NATIVE.fan()
        if node:
            value = s2native.number(s2native.read(node / "pwm1"))
            if value is not None:
                status = dict(status, duty_pct=str(round(value*100/255)))
    return status


def _thermal_zone_cpu_temp():
    """fan-control 没写 cpu_temp 时的兜底：直接读 /sys/class/thermal/thermal_zone*/temp。
    优先选 type 含 "cpu" 或 "soc" 的温区，找不到就退回 thermal_zone0；读不到返回 ""。
    单位从毫度转整数摄氏度，对齐 fan-control.sh 里 get_cpu_temp() 的换算方式。"""
    base = "/sys/class/thermal"
    if not os.path.isdir(base):
        return ""
    try:
        zones = sorted(d for d in os.listdir(base) if d.startswith("thermal_zone"))
    except OSError:
        return ""
    if not zones:
        return ""
    picked = ""
    for z in zones:
        ztype = sysfs_read(os.path.join(base, z, "type"), "").lower()
        if "cpu" in ztype or "soc" in ztype:
            picked = z
            break
    if not picked:
        picked = "thermal_zone0" if "thermal_zone0" in zones else zones[0]
    raw = sysfs_read(os.path.join(base, picked, "temp"), "")
    try:
        return int(int(raw) / 1000)
    except ValueError:
        return ""


def collect_sensor():
    """轻量快照：只读实时传感器，不碰配置、不扫脚本/模块目录。

    前端 10 秒轮询走这个端点，配置类信息走 /api/state。
    """
    fan_status = fan_snapshot()
    led_ovr = read_kv(LED_OVERRIDE_CONF)
    st = {
        "root": is_root(),
        "hostname": sysfs_read("/proc/sys/kernel/hostname", ""),
        "port": os.environ.get("PORT", ""),
        "agent": os.environ.get("S2_AGENT_URL", ""),
        "services": services_state(HW_SERVICES),
        "net_link": net_link_info(),
        "led_override": {
            "power": led_ovr.get("POWER_LED", "auto"),
            "sata": led_ovr.get("SATA_LED", "auto"),
        },
    }
    st.update(_fan_fields(fan_status))
    if not st.get("cpu_temp"):
        # fan-control 服务未跑/没来得及写 status 时，前端的 CPU 温度不应该空着。
        # 只在这个轻量快照端点兜底，collect_status() 的 _fan_fields 映射不受影响。
        st["cpu_temp"] = _thermal_zone_cpu_temp()
    st.update(_sys_fields())
    return st


def _sys_fields():
    """系统资源快照：内存、CPU 占用、负载、网络速率、磁盘清单。
    挂在 /api/sensor（10 秒轮询）上：全是读 /proc、/sys 的文件 IO，不起进程
    （HW_SPEC 硬规则：这个端点不能有子进程、不能碰 SMART，目标 < 50ms）。

    cpu_pct / mem / load / net / disk_list 是老字段，原样保留（前端依赖）；
    cpu / nics / fs 是 HW_SPEC 新增的扩展字段，取自 hwinfo 模块里同样「只读
    /proc /sys、不起子进程」的那部分函数（per_core/mhz/温度/休眠判定），
    hwinfo 里涉子进程或 SG_IO 的 SMART 相关函数这里绝不会调用。
    """
    # read_meminfo_ext() 是 read_meminfo() 的超集（同一套 total/avail/used 公式，
    # 多算 cached_kb/buffers_kb/free_kb/swap_*），直接用它替换旧的 mem 字段——
    # 老字段 total_kb/avail_kb/used_kb/used_pct 原样保留，前端不用改。
    mem = hwinfo.read_meminfo_ext() or read_meminfo() or _meminfo_fallback()
    cpu_temp = _thermal_zone_cpu_temp()
    disk_list = list_disks()
    for d in disk_list:
        try:
            d["standby"] = hwinfo.disk_is_standby("/dev/" + d["name"], d["name"], d["rotational"])
        except Exception:
            d["standby"] = None
        if d["standby"] is not False:
            d["temp"] = None
    # read_cpu_pct() 内部靠一份模块级基线算「两次采样之间」的差值——同一次轮询里
    # 调两遍，第二遍会把第一遍刚更新的基线当成「上一次」，算出几乎为 0 的占用率。
    # 所以只调一次，cpu.pct 与旧字段 cpu_pct 共享同一个值（HW_SPEC: "与 cpu_pct 同值"）。
    cpu_pct = read_cpu_pct()
    return {
        "mem": mem,
        "cpu_pct": cpu_pct,
        "load": read_loadavg(),
        "net": read_net_rate(),
        "disk_list": disk_list,
        "cpu": {
            "pct": cpu_pct,
            "per_core": hwinfo.read_cpu_per_core(),
            "mhz": hwinfo.cpu_mhz_list(),
            "temp": cpu_temp if isinstance(cpu_temp, int) else None,
            "load": hwinfo.read_cpu_load_triplet(),
        },
        "nics": hwinfo.read_nic_rates(),
        "fs": [{"mount": f["mount"], "pct": f["pct"], "used": f["used"], "total": f["total"]}
               for f in hwinfo.read_filesystems()],
    }


def collect_status():
    fan_status = fan_snapshot()
    # /proc/uptime 形如 "123456.78 987654.32"，只取开机秒数；读一次复用，别读两遍
    _up = sysfs_read("/proc/uptime", "").split() or _uptime_fallback()
    # 每个配置文件只读一次：原先 sched 读 3 遍、led_override 读 2 遍，
    # 一次轮询就是 5 次多余的 open+parse。
    led_ovr = read_kv(LED_OVERRIDE_CONF)

    # 模块只扫目录（纯文件 IO）。模块声明的服务并进同一次 systemctl show，
    # 无论装了多少模块，这里始终只起 1 个进程。
    mods = scan_patch_modules()
    svc = services_state(HW_SERVICES + module_services(mods))
    sched_ok, sched_info = s2hardware.dispatch(_sys.modules[__name__], "schedule_info", {})

    st = {
        "root": is_root(),
        "uid": current_uid(),
        "hostname": sysfs_read("/proc/sys/kernel/hostname", ""),
        "uptime": _up[0] if _up else "",
        # 顶栏时钟走设备时间：浏览器记下与本机的差，之后每秒本地推算，不为时钟多发请求。
        # tz_offset 是设备时区相对 UTC 的秒数（含夏令时），东八区 = 28800。
        "now": round(time.time(), 3),
        "tz_offset": getattr(time.localtime(), "tm_gmtoff", -time.timezone),
        # 一次 systemctl show 取回全部服务状态，不再每个服务单独起进程
        "services": {s: svc.get(s, "unknown") for s in HW_SERVICES},
        "leds": list_leds(),
        "conf": {
            "beep": load_app_conf(APP_BEEP_CONF),
            "sata_led": read_kv(ETC_SATA_LED),
            "fan": read_kv(ETC_FAN),
            "led_override": {
                "power": led_ovr.get("POWER_LED", "auto"),
                "sata": led_ovr.get("SATA_LED", "auto"),
            },
            "power_schedule": sched_info if sched_ok else {"enabled": False, "error": str(sched_info)},
        },
        "scripts": list_scripts(),
        "patches": [patch_view(m, svc) for m in mods],
        "patch_warnings": list(PATCH_SCAN_WARNINGS),
        "framework_ver": FRAMEWORK_VER,
        "app_ver": app_version(),
        "board": board_model(),
        "hardware_backend": "native" if NATIVE_ENABLED else "legacy",
        "native_hardware": NATIVE.capabilities(),
        "port": os.environ.get("PORT", ""),
        "agent": os.environ.get("S2_AGENT_URL", ""),
        "net_link": net_link_info(),
    }
    st.update(_fan_fields(fan_status))
    return st


# power-led-net.sh 的 link_iface() 忽略前缀/精确名单（见 firmware/power-led-net.sh
# 的 case 分支）。保持与它完全一致：这里只是把它从 bash 搬成 Python 读 sysfs，
# 判定逻辑必须原样对应，否则电源灯的物理状态和页面上显示的链路状态就会对不上。
_NET_LINK_SKIP_EXACT = ("lo",)
_NET_LINK_SKIP_PREFIX = ("docker", "br-", "br", "veth", "virbr", "vmnet", "tun", "tap",
                         "dummy", "ifb", "sit", "gre", "wg", "zt", "tailscale", "ppp")


def _net_link_iface():
    """等价于 power-led-net.sh 的 link_iface()：按 /sys/class/net 字典序找第一个
    carrier=1 的网卡。carrier 文件只有接口 up 时才可读，读不到视为无链路——
    与脚本里 `cat carrier 2>/dev/null || continue` 的语义一致。"""
    base = "/sys/class/net"
    if not os.path.isdir(base):
        return None
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for name in names:
        if name in _NET_LINK_SKIP_EXACT or any(name.startswith(p) for p in _NET_LINK_SKIP_PREFIX):
            continue
        carrier = sysfs_read(os.path.join(base, name, "carrier"), "")
        if carrier == "1":
            return name
    return None


def net_link_info():
    """电源灯网络状态灯的链路状态：纯 Python 读 sysfs，逻辑对应 power-led-net.sh
    的 status 子命令（原来每次都 fork 一次 bash 脚本，现在 0 子进程、纯文件 IO）。

    deployed 字段含义不变：脚本文件是否部署在设备上（即便不跑这个脚本，电源灯
    常驻服务也认这份判定逻辑，所以 deployed=False 时前端应显示「未部署」而不是
    「未连接」）。
    """
    out = {"up": None, "iface": "", "deployed": os.path.exists(POWER_LED_STATUS_BIN)}
    iface = _net_link_iface()
    out["up"] = iface is not None
    out["iface"] = iface or ""
    return out


def list_scripts():
    """列出可执行脚本。除 .sh 外也收 .run —— 单文件自解压安装包是模块热插拔入口。

    下划线开头的（_common.sh）是给别的脚本 source 的函数库，不是给人点的入口：
    单独跑它只会安静地退出 0，在列表里占一行只会让人以为「这个脚本坏了」。
    """
    out = []
    for d in [SCRIPTS_DIR, SHARE_SCRIPTS]:
        if not d or not os.path.isdir(d):
            continue
        for pat in ("*.sh", "*.run"):
            for f in sorted(glob.glob(os.path.join(d, pat))):
                name = os.path.basename(f)
                if name.startswith("_"):
                    continue
                try:
                    out.append({"name": name, "path": f,
                                "builtin": d == SCRIPTS_DIR,
                                "run": f.endswith(".run"),
                                "size": os.path.getsize(f),
                                "mtime": int(os.path.getmtime(f))})
                except OSError:
                    continue
    return out


# ---------------- 补丁模块框架 ----------------
def _ver_tuple(v):
    nums = []
    for p in str(v).strip().split(".")[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _ver_satisfies(need, have):
    """need 是否 <= have（语义版本，最多 3 段）"""
    return _ver_tuple(need) <= _ver_tuple(have)


def _inside(base, path):
    """path 是否落在 base 目录内（规范化后按路径分量比较，挡掉 ../ 与符号链接越界）"""
    try:
        b = os.path.realpath(base)
        p = os.path.realpath(path)
    except OSError:
        return False
    return p == b or p.startswith(b + os.sep)


def validate_patch_module(mid, kv, base):
    """校验一个补丁模块定义，返回错误列表（空=合法）。

    相对旧实现收紧的三处：
      · SERVICES 必须是合法 unit 名（旧实现只查「不含空白」，`foo;reboot` 能过）
      · ENABLE / DISABLE 每项必须是单段标识符（同上，杜绝 `x; reboot` / `$(...)`）
      · SCRIPT 必须落在模块目录内且真实存在（旧实现对绝对路径完全不校验，
        等于让任何能写 module.conf 的人指定 /bin/sh 去执行任意东西）
    """
    errs = []
    ident = kv.get("ID", "").strip()
    if not ident:
        errs.append("ID 缺失")
    elif not MODULE_ID_RE.match(ident):
        errs.append("ID 非法（须 %s）" % MODULE_ID_RE.pattern)
    elif ident != mid:
        errs.append("ID=%s 与目录名 %s 不一致" % (ident, mid))

    if not kv.get("TITLE", "").strip():
        errs.append("TITLE 缺失")
    code = kv.get("CODE", "PCH").strip() or "PCH"
    if not MODULE_CODE_RE.match(code):
        errs.append("CODE 非法（须 %s，默认 PCH）" % MODULE_CODE_RE.pattern)

    script = kv.get("SCRIPT", "").strip()
    if script:
        full = script if os.path.isabs(script) else os.path.join(base, script)
        if not _inside(base, full):
            errs.append("SCRIPT 越出模块目录: %s" % script)
        elif not os.path.isfile(full):
            errs.append("SCRIPT 不存在: %s" % script)

    for field in ("MODULE_VER", "REQUIRES"):
        v = kv.get(field, "").strip()
        if v and not SEMVER_RE.match(v):
            errs.append("%s 非法（应为语义版本如 1.0.0）: %s" % (field, v))

    for item in kv.get("SERVICES", "").split(","):
        s = item.strip()
        if s and not MODULE_UNIT_RE.match(s):
            errs.append("SERVICES 含非法服务名: %s" % s)

    for field in ("ENABLE", "DISABLE"):
        for item in kv.get(field, "").split(","):
            s = item.strip()
            if s and not MODULE_SUBCMD_RE.match(s):
                errs.append("%s 含非法子命令（只允许单段标识符）: %s" % (field, s))
    return errs


# scan_patch_modules() 缓存：按「PATCH_DIR 自身 mtime + 每个模块 module.conf 的
# mtime」算一个签名，签名不变就直接返回上次解析好的结果，不重新 read_kv +
# validate_patch_module。装/卸模块、改 module.conf 都会碰到这两种 mtime 之一，
# 缓存会自动失效；没人碰补丁目录时，10 秒一次的 /api/state 这里从「扫目录 +
# 解析 N 份 module.conf + 校验」降到「stat 目录 + N 次 stat」。
_PATCH_SCAN_CACHE = {"sig": None, "out": [], "warnings": []}


def _patch_scan_signature():
    try:
        dir_mtime = os.stat(PATCH_DIR).st_mtime
    except OSError:
        return None
    sig = [dir_mtime]
    try:
        names = sorted(os.listdir(PATCH_DIR))
    except OSError:
        return None
    for d in names:
        cf = os.path.join(PATCH_DIR, d, "module.conf")
        try:
            sig.append((d, os.stat(cf).st_mtime))
        except OSError:
            sig.append((d, None))
    return tuple(sig)


def scan_patch_modules():
    """扫描 PATCH_DIR，返回全部合法模块（按目录/module.conf 的 mtime 签名缓存）。

    坏 module.conf / 版本不兼容的模块被跳过并记入 PATCH_SCAN_WARNINGS。
    本函数只读文件，不执行任何模块脚本 —— 它挂在 10 秒一次的 /api/state 上。
    """
    global PATCH_SCAN_WARNINGS
    if not os.path.isdir(PATCH_DIR):
        PATCH_SCAN_WARNINGS = []
        return []
    sig = _patch_scan_signature()
    if sig is not None and sig == _PATCH_SCAN_CACHE["sig"]:
        PATCH_SCAN_WARNINGS = list(_PATCH_SCAN_CACHE["warnings"])
        return list(_PATCH_SCAN_CACHE["out"])

    PATCH_SCAN_WARNINGS = []
    out = []
    for d in sorted(os.listdir(PATCH_DIR)):
        base = os.path.join(PATCH_DIR, d)
        if not os.path.isdir(base) or not MODULE_ID_RE.match(d):
            continue
        cf = os.path.join(base, "module.conf")
        if not os.path.isfile(cf):
            PATCH_SCAN_WARNINGS.append("%s: 缺少 module.conf" % d)
            continue
        kv = read_kv(cf)
        errs = validate_patch_module(d, kv, base)
        if errs:
            PATCH_SCAN_WARNINGS.append("%s: 校验失败 -> %s" % (d, "; ".join(errs)))
            continue
        req = kv.get("REQUIRES", "0").strip() or "0"
        if not _ver_satisfies(req, FRAMEWORK_VER):
            PATCH_SCAN_WARNINGS.append(
                "%s: 需要框架 v%s，当前 v%s，已跳过" % (d, req, FRAMEWORK_VER))
            continue
        script = kv.get("SCRIPT", "").strip()
        if script and not os.path.isabs(script):
            script = os.path.join(base, script)
        conf = kv.get("CONF", "").strip()
        if conf and not os.path.isabs(conf):
            conf = os.path.join(base, conf)
        out.append({
            "id": d,
            "title": kv.get("TITLE", d),
            "code": kv.get("CODE", "PCH"),
            "desc": kv.get("DESC", ""),
            "script": script,
            "conf": conf,
            "services": [s.strip() for s in kv.get("SERVICES", "").split(",") if s.strip()],
            "enable": [s.strip() for s in kv.get("ENABLE", "").split(",") if s.strip()],
            "disable": [s.strip() for s in kv.get("DISABLE", "").split(",") if s.strip()],
            "module_ver": kv.get("MODULE_VER", ""),
            "requires": req,
        })
    _PATCH_SCAN_CACHE["sig"] = sig
    _PATCH_SCAN_CACHE["out"] = list(out)
    _PATCH_SCAN_CACHE["warnings"] = list(PATCH_SCAN_WARNINGS)
    return out


def module_services(mods):
    """把所有模块声明的服务名去重摊平，供 collect_status 一次性查状态。"""
    seen, out = set(), []
    for m in mods:
        for s in m.get("services", []):
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def patch_view(m, svc=None):
    """把一个模块折叠成前端可渲染的字典。

    svc 为 collect_status 已批量查好的 {服务名: 状态}；缺省时才单独查。
    注意：这里不执行模块脚本。旧实现在 patch_view 里为每个模块跑 2 次 `sh script status`，
    N 个模块 = 每次刷新 fork 2N 个 shell 且各带 timeout=15 —— 一个坏模块就能把
    /api/state 拖到分钟级。状态文本改由 op_patch(action=status) 按需拉取。
    """
    enabled = bool(m["conf"]) and os.path.exists(m["conf"])
    if svc is None:
        svc = services_state(m.get("services", []))
    return {
        "id": m["id"],
        "title": m["title"],
        "code": m["code"],
        "desc": m["desc"],
        "enabled": enabled,
        "deployed": bool(m["script"]) and os.path.isfile(m["script"]),
        "services": {s: svc.get(s, "unknown") for s in m.get("services", [])},
        "version": m.get("module_ver", ""),
    }


def _has_shebang(path):
    """首行是 #! 且不带 CR。带 +x 却没有 shebang（或 Windows 换行的 shebang）
    直接 exec 会得到 ENOEXEC / 找不到 "/bin/bash\\r"，报错还是英文。"""
    try:
        with open(path, "rb") as f:
            line = f.readline(256)
    except OSError:
        return False
    return line.startswith(b"#!") and not line.rstrip(b"\n").endswith(b"\r")


def _module_argv(script, sub):
    """构造执行模块脚本的 argv。可执行且有正常 shebang 才直接跑，否则交给 bash
    （.run 常见无 +x）。Windows 上 os.access(X_OK) 对任何文件都为真，一律走 bash。"""
    direct = os.name != "nt" and os.access(script, os.X_OK) and _has_shebang(script)
    head = [script] if direct else ["bash", script]
    return head + [sub]


_CJK_RE = re.compile(r"[一-鿿]")


def _module_fail(title, verb, msgs, out):
    """模块某一步失败：脚本自己说了中文就原样给用户；否则（Python 异常、
    "not found: bash"、英文报错）换成中文句子，原文放 detail。"""
    if _CJK_RE.search(out or ""):
        return False, "；".join(msgs)
    return False, msg_detail("模块「%s」%s失败" % (title, verb), "；".join(msgs))


def _run_module(script, sub, timeout=30):
    """执行模块脚本的一个子命令。sub 已过 MODULE_SUBCMD_RE，且不经 shell。"""
    if not script or not os.path.isfile(script):
        return 127, "模块脚本不存在"
    return run(_module_argv(script, sub), timeout=timeout)


def op_patch(params):
    """补丁模块操作。

    params: {id, action}
      action = status  查看模块自带状态文本
             = apply   按 ENABLE 序列启用
             = remove  按 DISABLE 序列停用
             = uninstall 彻底卸载（还原系统改动 + 删服务/规则 + 删目录）
    """
    mid = str(params.get("id") or "").strip()
    action = str(params.get("action") or "status").strip()
    if not MODULE_ID_RE.match(mid):
        return False, "模块 ID 非法：%s" % mid
    m = next((x for x in scan_patch_modules() if x["id"] == mid), None)
    if not m:
        return False, "未知补丁模块：%s" % mid

    if action == "uninstall":
        return _remove_module(m)
    if action == "status":
        if not m["script"]:
            return True, {"text": "该模块未绑定脚本，无状态可查"}
        rc, out = _run_module(m["script"], "status", timeout=15)
        return rc == 0, {"text": (out or "").strip()[:2000] or "（无输出）"}
    if action not in ("apply", "remove"):
        return False, "未知动作：%s" % action

    seq = m["enable"] if action == "apply" else m["disable"]
    if not seq:
        return False, "该模块未声明%s序列" % ("启用" if action == "apply" else "停用")
    if not m["script"]:
        return False, "该模块未绑定可执行脚本"

    msgs = []
    for a in seq:
        rc, out = _run_module(m["script"], a, timeout=30)
        if rc != 0:
            msgs.append((out or "").strip() or ("%s %s 失败（退出码 %d）" % (m["title"], a, rc)))
            bust_services_cache()  # 模块脚本可能在失败前已经动过某些服务
            return _module_fail(m["title"], "启用" if action == "apply" else "停用", msgs, out)
        msgs.append((out or "").strip() or ("%s %s 完成" % (m["title"], a)))
    bust_services_cache()  # 模块脚本常在 apply/remove 里 systemctl restart 自己声明的服务
    return True, "；".join(msgs)


def _remove_module(m):
    """彻底卸载一个模块，把系统还原到安装前状态。

    1) 跑 DISABLE 序列（要求幂等）还原补丁对系统的改动
    2) 停用并删除模块随附的 *.service / *.rules
    3) daemon-reload + udev 重载，最后删模块目录
    """
    msgs = []
    script = m.get("script", "")
    if script:
        for a in m.get("disable") or []:
            rc, out = _run_module(script, a, timeout=30)
            if rc != 0:
                msgs.append((out or "").strip() or ("还原 %s 失败（退出码 %d）" % (a, rc)))
                if _CJK_RE.search(out or ""):
                    return False, "还原失败，已中止：" + "；".join(msgs)
                return False, msg_detail("还原失败，已中止卸载", "；".join(msgs))
            msgs.append((out or "").strip() or ("还原 %s 完成" % a))

    for s in m.get("services") or []:
        if not MODULE_UNIT_RE.match(s):
            continue
        run(["systemctl", "disable", "--now", s], timeout=20)
        msgs.append("服务已停用: %s" % s)
    bust_services_cache()  # 卸载流程可能停用了模块声明的服务，缓存立刻失效

    for pat, sys_dir in (("*.service", "/etc/systemd/system"),
                         ("*.timer", "/etc/systemd/system"),
                         ("*.rules", "/etc/udev/rules.d")):
        for f in glob.glob(os.path.join(PATCH_DIR, m["id"], pat)):
            tgt = os.path.join(sys_dir, os.path.basename(f))
            if os.path.exists(tgt):
                try:
                    os.remove(tgt)
                    msgs.append("已删除: %s" % tgt)
                except OSError as e:
                    msgs.append("删除失败: %s (%s)" % (tgt, e))
    run(["systemctl", "daemon-reload"], timeout=20)
    run(["udevadm", "control", "--reload-rules"], timeout=20)

    mod_dir = os.path.join(PATCH_DIR, m["id"])
    if _inside(PATCH_DIR, mod_dir) and os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir, ignore_errors=True)
        msgs.append("模块目录已删除: %s" % mod_dir)
    return True, "；".join(msgs) or "模块已卸载"


# ---------------- 各 op 实现 ----------------
def op_status(params):
    return True, collect_status()


def op_hwinfo(params):
    """HW_SPEC 第 1 节的 HWINFO：慢变硬件信息，内部自带 60s/600s 两级缓存。
    params: {force: bool} —— 强制重采（仍然不会唤醒休眠盘，见 hwinfo 模块文档）。"""
    force = bool(params.get("force"))
    return True, hwinfo.collect_hwinfo(force=force)


def op_set_power_led(params):
    # v1.1.4 起电源灯由 power-led-net（网络状态灯）常驻接管；v1.1.5 起用 set_led_override 控制。
    # 保留函数以便旧版前端/CLI 调用时得到明确提示（未注册进 OPS）。
    return False, "电源灯已由网络状态灯接管；全灭/恢复请用「灯光覆盖」(set_led_override)"


def op_set_led_override(params):
    if not NATIVE_ENABLED and not legacy_controller_available():
        return False, "硬件控制尚未就绪，请先运行硬件自检并核对设备树"
    """手动覆盖：power = auto|off，sata = auto|breathe|off。

    off 优先级最高（固件守护每节拍重读本配置）。breathe 是「盘休眠时绿灯 2 秒亮 /
    2 秒灭」，需要硬盘灯服务装过「硬盘呼吸灯」补丁 —— 没装时服务不认这个值，
    灯会停在常亮，不会灭也不会崩，所以这里不拦。
    """
    updates = {}
    power = params.get("power")
    sata = params.get("sata")
    if power in ("auto", "off"):
        updates["POWER_LED"] = power
    if sata in ("auto", "off", "breathe"):
        updates["SATA_LED"] = sata
    if not updates:
        return False, "指示灯模式不对：电源灯只能是自动或全灭，硬盘灯只能是自动、休眠呼吸或全灭"
    write_kv(LED_OVERRIDE_CONF, updates,
             header="# S2硬件控制台 灯光手动覆盖（off=全灭优先级最高，breathe=休眠呼吸，auto=固件自动）")
    if NATIVE_ENABLED:
        return apply_native_settings("指示灯设置已保存")
    msg = []
    if "POWER_LED" in updates:
        v = updates["POWER_LED"]
        restart_service("power-led-net")
        msg.append("电源灯=%s（%s）" % (v, "全灭" if v == "off" else "网络状态自动"))
    if "SATA_LED" in updates:
        v = updates["SATA_LED"]
        restart_service("sata-led-manager")
        cn = {"off": "全灭", "breathe": "休眠呼吸", "auto": "有盘绿灯自动"}[v]
        msg.append("硬盘灯=%s（%s）" % (v, cn))
    return True, "已保存并立即生效：" + "，".join(msg)


def _as_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def op_set_fan(params):
    """风扇三档温控策略：quiet / performance / custom。

    v1.6 起改用「四锚点百分比线性插值」模型：
      温度 <= T1        → D1%（起转下限，固定 30）
      T1 ~ T2          → D1% → D2%
      T2 ~ T3          → D2% → D3%
      T3 ~ T4          → D3% → D4%（全速，固定 100）
      >= T4            → 100%
    低于 T1 直接停转（0%），所以曲线本身不需要「0 挡」。

    原来那套 0–15 挡位 + 手动模式已废弃：挡位到占空比不是正比（FAN_DUTY_MIN=60
    时 1 挡就是 60%），界面上「10 / 15 挡」和「85%」两个数永远对不上。
    现在直接把占空比当唯一真相，挡位这个概念不存在了。

    params:
      tempProfile: quiet|performance|custom
      hdd / cpu:   {t1,t2,t3,t4,d2,d3}
      D1=30 / D4=100 固定，不落盘。
    """
    if not NATIVE_ENABLED and not legacy_controller_available():
        return False, "硬件控制尚未就绪，请先运行硬件自检并核对设备树"
    profile = str(params.get("tempProfile") or "custom").strip().lower()
    if profile not in ("quiet", "performance", "custom"):
        return False, "风扇档位只能是静音、性能或自定义"
    hdd = params.get("hdd") or {}
    cpu = params.get("cpu") or {}

    h_t1 = _as_int(hdd.get("t1"), 42); h_t2 = _as_int(hdd.get("t2"), 48)
    h_t3 = _as_int(hdd.get("t3"), 52); h_t4 = _as_int(hdd.get("t4"), 55)
    h_d2 = _as_int(hdd.get("d2"), 30); h_d3 = _as_int(hdd.get("d3"), 70)
    c_t1 = _as_int(cpu.get("t1"), 60); c_t2 = _as_int(cpu.get("t2"), 65)
    c_t3 = _as_int(cpu.get("t3"), 72); c_t4 = _as_int(cpu.get("t4"), 80)
    c_d2 = _as_int(cpu.get("d2"), 30); c_d3 = _as_int(cpu.get("d3"), 70)

    # 锚点必须严格递增，否则固件里 curve_duty 的插值分母会变 0 或算出负数。
    # 上限跟硬件走：机械盘 55℃ 起老化明显加快（原来是 65℃），
    # CPU 80℃ 是这颗 SoC 的降频点。
    errs = []
    if h_t1 < 30:
        errs.append("硬盘 T1 不能低于 30℃")
    if h_t2 <= h_t1:
        errs.append("硬盘 T2 必须大于 T1")
    if h_t3 <= h_t2:
        errs.append("硬盘 T3 必须大于 T2")
    if h_t4 <= h_t3 or h_t4 > 55:
        errs.append("硬盘 T4 必须大于 T3 且 ≤ 55℃")
    if c_t1 < 40:
        errs.append("CPU T1 不能低于 40℃")
    if c_t2 <= c_t1:
        errs.append("CPU T2 必须大于 T1")
    if c_t3 <= c_t2:
        errs.append("CPU T3 必须大于 T2")
    if c_t4 <= c_t3 or c_t4 > 80:
        errs.append("CPU T4 必须大于 T3 且 ≤ 80℃")
    if not 30 <= h_d2 <= 100:
        errs.append("硬盘 T2 转速须在 30–100%")
    if not h_d2 <= h_d3 <= 100:
        errs.append("硬盘 T3 转速必须 ≥ T2 且 ≤ 100%")
    if not 30 <= c_d2 <= 100:
        errs.append("CPU T2 转速须在 30–100%")
    if not c_d2 <= c_d3 <= 100:
        errs.append("CPU T3 转速必须 ≥ T2 且 ≤ 100%")
    if errs:
        return False, "；".join(errs)

    if not os.path.exists(ETC_FAN):
        return False, "未找到 %s，风扇服务可能未安装" % ETC_FAN

    write_kv(ETC_FAN, {
        "TEMP_PROFILE": profile,
        "HDD_T1": str(h_t1), "HDD_T2": str(h_t2), "HDD_T3": str(h_t3), "HDD_T4": str(h_t4),
        "HDD_D2": str(h_d2), "HDD_D3": str(h_d3),
        "CPU_T1": str(c_t1), "CPU_T2": str(c_t2), "CPU_T3": str(c_t3), "CPU_T4": str(c_t4),
        "CPU_D2": str(c_d2), "CPU_D3": str(c_d3),
    })

    cn = {"quiet": "节能/安静", "performance": "性能", "custom": "自定义"}[profile]
    if NATIVE_ENABLED:
        return apply_native_settings("风扇曲线已保存")
    # 新版 fan-control.sh 认 reload 标记：0.25 秒内重读曲线、跳过去抖立刻出风，不重启服务。
    # 以前每次都 systemctl restart：等 systemd 停起、init_pwm 先关风扇再开，点一下要好几秒才有反应。
    # 设备上还是旧脚本（没升级固件）时才退回重启服务。
    if _fan_script_has("RELOAD_FILE") and service_active("fan-control") == "active":
        try:
            os.makedirs(FAN_RUN_DIR, exist_ok=True)
            with open(FAN_RELOAD, "w") as f:
                f.write("%d\n" % int(time.time()))
            return True, "风扇策略已切换为「%s」，已立即生效" % cn
        except OSError:
            pass                      # 写不进去就走下面的重启
    ok, out = restart_service("fan-control")
    if not ok:
        return False, msg_detail("曲线已保存，但风扇服务重启失败，新曲线要等服务重启后才生效", out)
    return True, "风扇策略已切换为「%s」，曲线已保存并生效" % cn


def _fan_script_has(marker):
    """设备上装的 fan-control.sh 是不是认得某个新功能（按脚本里有没有这个变量名判断）。"""
    if NATIVE_ENABLED:
        return NATIVE.capabilities()["ready"] and marker in ("force_until", "RELOAD_FILE", "GUARD_CONF")
    try:
        with open(FAN_SCRIPT, "r", encoding="utf-8", errors="ignore") as f:
            return marker in f.read()
    except OSError:
        return False


FAN_SCRIPT = os.environ.get("S2_FAN_SCRIPT", "/usr/local/bin/fan-control.sh")
FAN_FORCE = os.path.join(FAN_RUN_DIR, "force_until")
FAN_RELOAD = os.path.join(FAN_RUN_DIR, "reload")


def legacy_controller_available():
    return os.path.isfile(FAN_SCRIPT) and os.path.isfile(ETC_FAN)


def apply_native_settings(message):
    if not NATIVE.capabilities()["ready"]:
        return False, NATIVE.capabilities()["detail"]
    if service_active(s2native.SERVICE) != "active":
        ok, detail = restart_service(s2native.SERVICE)
        if not ok: return False, message + "，原生硬件服务未启动，请查看诊断"
    return True, message + "，原生硬件服务将在下一次采样应用"


def restart_hardware_services():
    names = [s2native.SERVICE] if NATIVE_ENABLED else ["fan-control", "power-led-net"]
    for name in names: restart_service(name)


def op_fan_test(params):
    """风扇全速测试：写 force_until（到期 epoch 秒），fan-control.sh 每 3 秒看一次，
    到期前 100%、不去抖，到期自己回温控。

    以前是临时把 CPU 曲线压到当前温度以下再重启服务：T1 有 40℃ 下限，设备凉的时候
    （CPU 35℃）当前温度根本够不到 T1，风扇反而停转；每次重启服务又先停转一轮去抖。
    sec=0 立即结束测试。
    """
    sec = max(0, min(60, _as_int(params.get("sec", 10), 10)))
    if NATIVE_ENABLED and sec:
        ok, message = apply_native_settings("准备风扇全速测试")
        if not ok: return False, message
    if not _fan_script_has("force_until"):
        return False, "风扇控制尚未支持全速测试，请先核对设备树与硬件控制服务"
    until = int(time.time()) + sec if sec else 0
    try:
        os.makedirs(FAN_RUN_DIR, exist_ok=True)
        tmp = FAN_FORCE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("%d\n" % until)
        os.replace(tmp, FAN_FORCE)
    except OSError as e:
        return False, msg_detail("写入全速测试指令失败", str(e))
    if not sec:
        return True, "已结束全速测试"
    return True, {"until": until, "sec": sec}


def op_set_beep(params):
    ms = max(30, min(5000, _as_int(params.get("ms", 280), 280)))
    conf = load_app_conf(APP_BEEP_CONF)
    conf["beep_ms"] = ms
    conf["boot_beep"] = bool(params.get("boot_beep", conf.get("boot_beep", True)))
    save_app_conf(APP_BEEP_CONF, conf)
    return True, "蜂鸣时长已保存为 %d ms" % ms


def op_beep_test(params):
    ms = params.get("ms")
    if ms is None:
        ms = load_app_conf(APP_BEEP_CONF).get("beep_ms", 280)
    return beep(ms)


_SERVICE_ACTION_ZH = {"start": "启动", "stop": "停止", "restart": "重启", "reload": "重新加载",
                      "enable": "设为自启", "disable": "取消自启", "reset-failed": "清除故障状态",
                      "try-restart": "尝试重启"}


def op_service(params):
    name = params.get("name", "")
    action = params.get("action", "restart")
    if name not in HW_SERVICES or not safe_name(name):
        return False, msg_detail("不允许操作该服务", name)
    if action not in SERVICE_ACTIONS:
        return False, msg_detail("不允许该操作", action)
    rc, out = run(["systemctl", action, name], timeout=40)
    action_zh = _SERVICE_ACTION_ZH.get(action, action)
    bust_services_cache()  # 服务状态刚被改变（这条不经过 restart_service()，单独补一次）
    if rc == 0:
        return True, msg_detail("%s%s已完成" % (name, action_zh), out.strip())
    return False, msg_detail("%s%s失败" % (name, action_zh), out.strip())


def op_logs(params):
    name = params.get("name", "sata-led")
    if NATIVE_ENABLED and name in ("sata-led", "fan", "power-led", "selfheal"):
        unit = "fnos-s2-dtb-guard.service" if name == "selfheal" else s2native.SERVICE
        count = max(1, min(1000, _as_int(params.get("n", 120), 120)))
        code, output = run(["journalctl", "-u", unit, "-n", str(count), "--no-pager"], timeout=10)
        return (True, {"lines": output.splitlines()}) if not code else (False, "无法读取原生服务日志，请检查日志权限")
    paths = {
        "sata-led": "/var/log/sata-led.log",
        "fan": "/var/log/fan_control.log",          # fan-control.sh 的 LOG_FILE 是下划线
        "power-led": "/var/log/power-led-net.log",
        "selfheal": "/var/log/selfheal-install.log",
        "app": os.path.join(VAR_DIR, "server.log"),
    }
    p = paths.get(name, "")
    if not p or not os.path.exists(p):
        return True, {"lines": ["(暂无日志) %s" % (p or name)]}
    n = max(1, min(1000, _as_int(params.get("n", 120), 120)))
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()[-n:]
    except OSError as e:
        return False, "读取日志失败：%s" % e
    return True, {"lines": lines}


def op_set_port(params):
    """改应用自身端口：写 settings.conf 再由 server 触发重启

    下限必须是 1024：低于它需要 root 才能 bind，而应用未必以 root 运行；
    真改到 80 再重启，服务起不来就再也进不去控制台了 —— 这是最不可逆的操作。
    前端也校验 1024–65535，两边必须一致，不能只靠界面。
    """
    port = _as_int(params.get("port", 0), 0)
    if port < 1024 or port > 65535:
        return False, "端口非法（允许范围 1024 – 65535）"
    etc = os.environ.get("S2_ETC_DIR", "/var/apps/fnOS-S2/etc")
    settings = os.path.join(etc, "settings.conf")
    write_kv(settings, {"PORT": port})
    return True, "端口已写入 %d，正在重启应用…" % port


OPS = {
    "status": op_status,
    "hwinfo": op_hwinfo,
    "set_led_override": op_set_led_override,
    "set_fan": op_set_fan,
    "fan_test": op_fan_test,
    "set_beep": op_set_beep,
    "beep_test": op_beep_test,
    "set_power_schedule": op_set_power_schedule,
    "power": op_power,
    "service": op_service,
    "logs": op_logs,
    "set_port": op_set_port,
    "patch": op_patch,
}


def dispatch(op, params):
    if op == "integration_status":
        try:
            units = services_state([s2native.SERVICE, "fnos-s2-dtb-guard.path", "fnos-s2-led-boot", "fnos-s2-led-shutdown"])
            return True, {"backend": "native" if NATIVE_ENABLED else "legacy" if legacy_controller_available() else "unavailable",
                "native": NATIVE.capabilities(), "dtb": s2dtb.state(NATIVE.root), "services": units,
                "lights_enabled": (NATIVE.conf/"led-beautify.enabled").exists(),
                "rollback_available": (NATIVE.root/"var/lib/fnos-s2/last-dtb-backup").is_file()}
        except (OSError, ValueError):
            return False, "无法读取设备树与原生硬件状态，请检查权限"
    if op in s2hardware.OPS:
        return s2hardware.dispatch(_sys.modules[__name__], op, params)
    fn = OPS.get(op)
    if not fn:
        return False, "未知操作：%s" % op
    try:
        if op.startswith("set_"):
            with s2hardware.LOCK:
                return fn(params or {})
        return fn(params or {})
    except Exception as e:  # noqa
        return False, msg_detail("执行时发生内部错误", str(e))
