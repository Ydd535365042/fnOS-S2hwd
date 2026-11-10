#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hwinfo —— S2 硬件控制台的「慢变硬件信息」采集层

设计要点
--------
1) 采集逻辑移植自用户自己的 jkmon 项目（C:\\Users\\Administrator\\Desktop\\JK\\jkmon\\
   linux_cpu.py / linux_cpuid.py / linux_sys.py / linux_io.py / linux_disk.py /
   disk_id.py / smart.py / smart_parse.py / linux_hw.py），按「移植不引用」的原则重写：
   S2 必须保持自包含，不 import jkmon 包。只保留 ARM Debian NAS 用得上的部分——
   没有 GPU/NPU 卡片、没有电池、没有 NVIDIA、没有 SMBIOS DIMM（板载内存多半为空）。
2) 纯函数（parse_*）只做文本/字节解析，不碰文件系统，方便离线单元测试
   （开发机是 Windows，没有 /proc /sys，测试用自己写的 Linux 文本样本）。
   采集函数（collect_* / _read_*）才真正 open() 文件或起子进程，失败即返回空值，
   绝不向上抛异常——延续 hwctl.py 现有风格。
3) 休眠盘绝不唤醒：SMART 读取前一律先做 ATA CHECK POWER MODE（SG_IO）判定，
   或 smartctl 走 `-n standby`；处于 standby 就直接跳过，只报告 standby=true。
4) 外部命令（smartctl/hdparm）只在 shutil.which 找得到时以 argv 调用并带超时；
   SG_IO / NVMe admin ioctl 需要 root 且 fcntl/ctypes 的用法是 Linux-only，
   因此惰性导入（函数内部 import），保证本模块在 Windows 开发机上也能正常
   import、能跑通纯函数测试。
5) 缓存：非 SMART 部分 60 秒，SMART 部分 600 秒，模块级缓存 + 一把锁
   （ThreadingHTTPServer 多线程同时轮询 /api/hwinfo 时必须互斥，否则会
   并发起好几份子进程/ioctl，且都在抢同一份缓存字典）。
"""

import glob
import importlib.util
import json
import os
import platform
import re
import struct
import shutil
import subprocess
import threading
import time

_disk_spec = importlib.util.spec_from_file_location("s2_disk", os.path.join(os.path.dirname(__file__), "..", "firmware", "s2_disk.py"))
disk_reader = importlib.util.module_from_spec(_disk_spec)
_disk_spec.loader.exec_module(disk_reader)

# ================================================================
# 基础工具（不依赖 hwctl，保持本模块可独立单测/单独 import）
# ================================================================

def rd(path, default=None):
    """读文本文件并 strip；读不到返回 default。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def rd_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def rd_int(path):
    v = rd(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def run(argv, timeout=10):
    """argv 形式子进程调用，不经 shell。返回 (rc, stdout+stderr)；失败不抛异常。"""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "not found: %s" % argv[0]
    except Exception as e:  # noqa
        return 1, str(e)


def which(name):
    return shutil.which(name)


_NET_IGNORE = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap")


# ================================================================
# CPU —— 移植自 jkmon linux_cpuid.py（ARM 核心型号表）+ linux_cpu.py（静态信息/频率）
# 只保留 ARM / x86 两条路径；服务器 ARM 的 SMBIOS 品牌识别、LoongArch/MIPS/RISC-V/
# POWER 分支在目标机型（RK3568）上用不到，一律删除，只留 ARM 核心表 + x86 model name。
# ================================================================

# 编码来自 Linux 内核 arch/arm64/include/asm/cputype.h 与 util-linux lscpu-arm.c
ARM_IMPL = {0x41: "ARM", 0x42: "Broadcom", 0x43: "Cavium", 0x46: "Fujitsu", 0x48: "HiSilicon",
            0x4E: "NVIDIA", 0x50: "APM", 0x51: "Qualcomm", 0x53: "Samsung", 0x56: "Marvell",
            0x61: "Apple", 0x66: "Faraday", 0x69: "Intel", 0x6D: "Microsoft", 0x70: "Phytium",
            0xC0: "Ampere"}
ARM_PARTS = {
    0x41: {0xC07: "Cortex-A7", 0xC08: "Cortex-A8", 0xC09: "Cortex-A9", 0xC0D: "Cortex-A17",
           0xC0F: "Cortex-A15", 0xD01: "Cortex-A32", 0xD03: "Cortex-A53", 0xD04: "Cortex-A35",
           0xD05: "Cortex-A55", 0xD06: "Cortex-A65", 0xD07: "Cortex-A57", 0xD08: "Cortex-A72",
           0xD09: "Cortex-A73", 0xD0A: "Cortex-A75", 0xD0B: "Cortex-A76", 0xD0C: "Neoverse-N1",
           0xD0D: "Cortex-A77", 0xD0E: "Cortex-A76AE", 0xD40: "Neoverse-V1", 0xD41: "Cortex-A78",
           0xD42: "Cortex-A78AE", 0xD44: "Cortex-X1", 0xD46: "Cortex-A510", 0xD47: "Cortex-A710",
           0xD48: "Cortex-X2", 0xD49: "Neoverse-N2", 0xD4B: "Cortex-A78C", 0xD4D: "Cortex-A715",
           0xD4E: "Cortex-X3", 0xD4F: "Neoverse-V2", 0xD80: "Cortex-A520", 0xD81: "Cortex-A720",
           0xD82: "Cortex-X4"},
    0x48: {0xD01: "TaiShan v110（鲲鹏 920）", 0xD02: "TaiShan v120", 0xD40: "Cortex-A76",
           0xD41: "Cortex-A77"},
}
# 内核对 ARM 只给出这种通用描述，不是型号
_GENERIC_MODEL = re.compile(r"^(armv\d+\w*|aarch64|arm)\s+processor(\s+rev\s+\d+.*)?$|^processor$|^\d+$", re.I)
_DT_VENDOR = {"rockchip": "Rockchip", "allwinner": "Allwinner", "amlogic": "Amlogic",
              "broadcom": "Broadcom", "qcom": "Qualcomm", "mediatek": "MediaTek",
              "nxp": "NXP", "fsl": "NXP", "ti": "TI", "marvell": "Marvell"}
_X86_VENDOR = {"GenuineIntel": "Intel", "AuthenticAMD": "AMD"}
_X86_ARCH = ("x86_64", "i386", "i486", "i586", "i686", "amd64")


def _hex(v):
    try:
        return int(v, 0)
    except (TypeError, ValueError):
        return None


def arm_core_name(impl, part):
    """单个核心的名称（如 Cortex-A55）；查不到时给出编码本身，不猜。"""
    name = ARM_PARTS.get(impl, {}).get(part)
    if name:
        return name
    if impl is None or part is None:
        return None
    return "%s 0x%03x" % (ARM_IMPL.get(impl, "0x%02x" % impl), part)


def parse_cpuinfo(txt):
    """解析 /proc/cpuinfo 文本。

    返回 {model, vendor, cores:[(impl,part), ...]}：
      - x86：model 取「model name」
      - ARM：没有「model name」字段，只有 CPU implementer / CPU part；
        model 由 arm_core_name() 按出现次数分组拼出（如 "4× Cortex-A55"）。
    读不到 / 空文本返回 model=None。
    """
    first, cores = {}, []
    for blk in re.split(r"\n\s*\n", txt or ""):
        kv = {}
        for line in blk.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                kv[k.strip().lower()] = v.strip()
        for k, v in kv.items():
            first.setdefault(k, v)
        if "cpu implementer" in kv and "cpu part" in kv:
            cores.append((_hex(kv["cpu implementer"]), _hex(kv["cpu part"])))

    vendor = _X86_VENDOR.get(first.get("vendor_id", ""), first.get("vendor_id") or None)
    model = None
    v = first.get("model name")
    if v and not _GENERIC_MODEL.match(v):
        model = re.sub(r"\s+", " ", v)

    core_name = None
    if not model and cores:
        names = [arm_core_name(i, p) for i, p in cores]
        groups = []
        for n in names:
            if groups and groups[-1][0] == n:
                groups[-1][1] += 1
            else:
                existing = next((g for g in groups if g[0] == n), None)
                if existing:
                    existing[1] += 1
                else:
                    groups.append([n, 1])
        model = groups[0][0] if len(groups) == 1 else \
            " + ".join("%d× %s" % (c, n) for n, c in groups)
        core_name = groups[0][0] if groups else None
        vendor = vendor or ARM_IMPL.get(cores[0][0])

    if not model:
        v = first.get("processor")
        if v and not v.isdigit():
            model = v

    return {"model": model, "core_name": core_name, "vendor": vendor, "cores": cores,
            "hardware": first.get("hardware")}


def dt_soc(compatible_bytes):
    """设备树 compatible（\\0 分隔字节串，最后一项一般是 SoC）→ "Rockchip RK3568"。"""
    items = [x for x in (compatible_bytes or b"").decode("ascii", "replace").split("\0") if x]
    if not items:
        return None
    v, _, n = items[-1].partition(",")
    if not n:
        return None
    if re.match(r"^[a-z]+\d+[a-z0-9]*$", n):
        n = n.upper()
    return "%s %s" % (_DT_VENDOR.get(v, v), n)


def cpu_physical_cores():
    """物理核数：线程兄弟列表相同的逻辑处理器属于同一核心（大小核场景也适用）。
    RK3568 是 4 个同构 A55、无超线程，这里始终等于逻辑核数，但保留通用实现。"""
    cores = set()
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology"):
        sib = rd(d + "/core_cpus_list") or rd(d + "/thread_siblings_list")
        if sib:
            cores.add(sib)
    return len(cores) or None


def cpu_mhz_list():
    """每个逻辑 CPU 的当前频率（MHz，int）。读不到 cpufreq 时退回 /proc/cpuinfo 的 "cpu MHz"（不常见于 ARM）。"""
    out = []
    paths = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"),
                    key=lambda x: int(re.search(r"cpu(\d+)", x).group(1)))
    for d in paths:
        v = None
        for name in ("scaling_cur_freq", "cpuinfo_cur_freq"):
            v = rd_int(os.path.join(d, name))
            if v:
                break
        out.append(int(v / 1000) if v else None)
    if any(out):
        return out
    vals = []
    for line in (rd("/proc/cpuinfo", "") or "").splitlines():
        if line.lower().startswith("cpu mhz"):
            try:
                vals.append(int(float(line.split(":")[1])))
            except (IndexError, ValueError):
                pass
    return vals


def cpu_static():
    """CPU 静态信息：型号/核心名/厂商/架构/核数/频率范围/调速器。

    ARM 分支：/proc/cpuinfo 本身只有核心微架构名（Cortex-A55），没有 SoC
    产品型号；真正的 "Rockchip RK3568" 来自设备树 compatible 最后一项，
    与核心名拼成 "Rockchip RK3568"（model）+ "Cortex-A55"（core_name）两个
    独立字段，交给前端自己组合摘要文案（HW_SPEC 的 #cpu-model 摘要）。
    """
    info = parse_cpuinfo(rd("/proc/cpuinfo", "") or "")
    model, vendor, core_name = info["model"], info["vendor"], info["core_name"]
    arch = platform.machine().lower()

    soc = None
    if arch not in _X86_ARCH:
        soc = dt_soc(rd_bytes("/proc/device-tree/compatible")) or info.get("hardware")
        if soc:
            # model 此时是核心架构描述（如 "4× Cortex-A55"）；SoC 型号另给一个字段，
            # 前端自己拼 "Rockchip RK3568 · 4 核 Cortex-A55"（见 HW_SPEC #cpu-model）。
            model = soc
    if not model and which("lscpu"):
        rc, out = run(["lscpu"], 5)
        m = re.search(r"^(?:Model name|型号名称)\s*[:：]\s*(.+)$", out, re.M)
        if m and m.group(1).strip() != "-":
            model = m.group(1).strip()

    cores = cpu_physical_cores() or len(info["cores"]) or os.cpu_count()
    threads = os.cpu_count()

    mx = [rd_int(p) for p in glob.glob("/sys/devices/system/cpu/cpufreq/policy*/cpuinfo_max_freq")]
    mn = [rd_int(p) for p in glob.glob("/sys/devices/system/cpu/cpufreq/policy*/cpuinfo_min_freq")]
    mx = [x for x in mx if x]
    mn = [x for x in mn if x]
    base = "/sys/devices/system/cpu/cpu0/cpufreq/"

    flags = []
    v = re.search(r"^flags\s*:\s*(.+)$", rd("/proc/cpuinfo", "") or "", re.M) or \
        re.search(r"^Features\s*:\s*(.+)$", rd("/proc/cpuinfo", "") or "", re.M)
    if v:
        flags = v.group(1).split()[:16]

    return {
        "model": model, "core_name": core_name, "vendor": vendor or (ARM_IMPL.get(0x41) if arch not in _X86_ARCH else None),
        "arch": arch, "cores": cores, "threads": threads,
        "max_mhz": int(max(mx) / 1000) if mx else None,
        "min_mhz": int(min(mn) / 1000) if mn else None,
        "governor": rd(base + "scaling_governor") or "",
        "cache": "",
        "flags": flags,
    }


# ---- 每核占用率（供 /api/sensor 的 cpu.per_core，10 秒轮询用，首次无基线返回 None） ----
_PERCORE_PREV = {}  # {cpu_label: (total, idle)}
_PERCORE_LOCK = threading.Lock()


def parse_proc_stat_percpu(txt):
    """解析 /proc/stat 里 "cpu0".."cpuN" 各行，返回 {label: (total_jiffies, idle_jiffies)}。
    纯函数，便于离线测试；不含首行的聚合 "cpu " 统计（那个是全局 cpu_pct，走 hwctl.read_cpu_pct）。"""
    out = {}
    for line in (txt or "").splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        parts = line.split()
        if len(parts) < 5 or not re.match(r"^cpu\d+$", parts[0]):
            continue
        try:
            nums = [int(x) for x in parts[1:]]
        except ValueError:
            continue
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        out[parts[0]] = (sum(nums), idle)
    return out


def read_cpu_per_core():
    """每核占用率 %列表，按 cpu0..cpuN 顺序；首次调用（无基线）各项为 None。"""
    cur = parse_proc_stat_percpu(rd("/proc/stat", "") or "")
    if not cur:
        return []
    labels = sorted(cur.keys(), key=lambda s: int(s[3:]))
    out = []
    with _PERCORE_LOCK:
        for lb in labels:
            total, idle = cur[lb]
            prev = _PERCORE_PREV.get(lb)
            _PERCORE_PREV[lb] = (total, idle)
            if not prev:
                out.append(None)
                continue
            d_total = total - prev[0]
            d_idle = idle - prev[1]
            if d_total <= 0:
                out.append(None)
                continue
            out.append(round((1 - d_idle / d_total) * 100.0, 1))
    return out


def read_cpu_load_triplet():
    """/proc/loadavg 的 1/5/15 分钟负载，列表 [float,float,float]；读不到 []。"""
    v = rd("/proc/loadavg", "")
    if not v:
        return []
    try:
        return [float(x) for x in v.split()[:3]]
    except ValueError:
        return []


# ================================================================
# 系统信息 —— 移植自 jkmon linux_sys.py（os-release / 型号 / 内核 / 架构 / 开机时间）
# SMBIOS DIMM 解析（dimms()）对 ARM 板载内存没有意义，不移植；型号用设备树 model
# （与 hwctl.board_model() 的逐级兜底不同：这里只给 HWINFO.system.board 一个值，
#  不影响 hwctl 已有的 board_model() / /api/state）。
# ================================================================

def parse_os_release(txt):
    """/etc/os-release → PRETTY_NAME（没有则 NAME，再没有则 "Linux"）。"""
    info = {}
    for line in (txt or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip().strip('"')
    return info.get("PRETTY_NAME") or info.get("NAME") or "Linux"


def read_os_release():
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        txt = rd(p)
        if txt:
            return parse_os_release(txt)
    return "Linux"


def read_board_model():
    """设备树 model（NUL 结尾裸字符串，需剥 \\0），读不到为空串。"""
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        raw = rd_bytes(path)
        if raw:
            v = raw.decode("utf-8", "replace").strip("\x00").strip()
            if v:
                return v
    return ""


def read_boot_ts():
    """开机时刻（秒，int）：now - uptime。读不到 /proc/uptime 时返回 None。"""
    v = rd("/proc/uptime", "")
    if not v:
        return None
    try:
        up = float(v.split()[0])
    except (ValueError, IndexError):
        return None
    return int(time.time() - up)


def system_static():
    return {
        "hostname": rd("/proc/sys/kernel/hostname", "") or platform.node(),
        "board": read_board_model(),
        "os": read_os_release(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "boot_ts": read_boot_ts(),
    }


# ================================================================
# 内存 —— 移植自 jkmon linux_cpu.meminfo()，裁剪出 HWINFO.memory 与 /api/sensor 扩展字段
# ================================================================

def parse_meminfo(txt):
    """/proc/meminfo → {TotalKb, AvailKb, FreeKb, BuffersKb, CachedKb, SwapTotalKb, SwapFreeKb}（单位 KB，int）。"""
    kv = {}
    for line in (txt or "").splitlines():
        k, _, v = line.partition(":")
        p = v.split()
        if p:
            try:
                kv[k.strip()] = int(p[0])
            except ValueError:
                pass
    return kv


def read_meminfo_ext():
    """扩展版内存快照：total_kb/avail_kb/used_kb/used_pct（与 hwctl.read_meminfo 同口径，不改其行为）
    + cached_kb/buffers_kb/free_kb/swap_total_kb/swap_used_kb（HW_SPEC 新增字段）。
    读不到返回空 dict。"""
    kv = parse_meminfo(rd("/proc/meminfo", "") or "")
    total = kv.get("MemTotal", 0)
    if not total:
        return {}
    free = kv.get("MemFree", 0)
    buffers = kv.get("Buffers", 0)
    cached = kv.get("Cached", 0)
    avail = kv.get("MemAvailable") or (free + buffers + cached)
    used = max(0, total - avail)
    swap_total = kv.get("SwapTotal", 0)
    swap_free = kv.get("SwapFree", 0)
    return {
        "total_kb": total, "avail_kb": avail, "used_kb": used,
        "used_pct": round(used * 100.0 / total, 1),
        "cached_kb": cached, "buffers_kb": buffers, "free_kb": free,
        "swap_total_kb": swap_total, "swap_used_kb": max(0, swap_total - swap_free),
    }


def memory_static():
    """HWINFO.memory：total（字节）、swap_total（字节）、dimms（ARM 板载内存多半为空，不读 SMBIOS）。"""
    kv = parse_meminfo(rd("/proc/meminfo", "") or "")
    total_kb = kv.get("MemTotal", 0)
    swap_kb = kv.get("SwapTotal", 0)
    return {"total": total_kb * 1024 if total_kb else 0,
            "swap_total": swap_kb * 1024 if swap_kb else 0,
            "dimms": []}


# ================================================================
# 文件系统 —— 移植自 jkmon linux_io.filesystems()
# ================================================================

_FS_KINDS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "vfat", "exfat",
             "ntfs", "ntfs3", "fuseblk", "jfs", "reiserfs", "bcachefs"}


def parse_mounts(txt):
    """/proc/mounts → [(dev, mount, fstype), ...]，已过滤掉伪文件系统（tmpfs/overlay/proc/… 不在 _FS_KINDS 表里自然被排除）。
    挂载点里的 \\040 转义还原为空格。按 dev 去重，同一设备多个挂载点保留路径最短的那个（bind mount 噪音）。"""
    seen = {}
    for line in (txt or "").splitlines():
        f = line.split()
        if len(f) < 3 or f[2] not in _FS_KINDS:
            continue
        dev, mnt = f[0], f[1].replace("\\040", " ")
        if not dev.startswith("/dev/") and not (f[2] == "zfs" and "/" not in dev):
            continue
        if dev in seen and len(seen[dev][1]) <= len(mnt):
            continue
        seen[dev] = (dev, mnt, f[2])
    return sorted(seen.values(), key=lambda x: x[1])


def _parent_disk(dev):
    """分区设备名 → 所属整盘名（sda1 → sda）；取不到就原样返回。"""
    base = os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else os.path.basename(dev)
    p = "/sys/class/block/" + base
    if os.path.exists(p + "/partition"):
        try:
            return os.path.basename(os.path.dirname(os.path.realpath(p)))
        except OSError:
            pass
    return base


def _holders_disks(dev_name, depth=0):
    """dm-* / md* 这类虚拟块设备 → 底下真正的整盘名集合（顺着 /sys/class/block/X/slaves 往下找）。
    LVM 在 RAID 上、加密卷在 LVM 上这种叠几层的也能找到；最多 6 层防环。"""
    if depth > 6:
        return set()
    sl = "/sys/class/block/%s/slaves" % dev_name
    try:
        kids = os.listdir(sl)
    except OSError:
        kids = []
    if not kids:
        return {_parent_disk("/dev/" + dev_name)}
    out = set()
    for k in kids:
        out |= _holders_disks(k, depth + 1)
    return out


def disk_usage_by_disk(filesystems):
    """每块整盘的「已挂载容量」汇总：{disk: {total, used, free, mounts:[...], shared:bool}}。

    普通分区直接算到所在盘。阵列 / LVM（dm-*, md*）上的卷属于好几块盘，
    同一份容量不能每块盘各算一遍（两块 4T 做 RAID1，不能显示成每块盘都 4T 已用），
    所以给每块成员盘记同一份卷的数字并标 shared=True，前端写「阵列共享」。"""
    out = {}
    for fs in filesystems or []:
        dev = fs.get("dev") or ""
        base = os.path.basename(os.path.realpath(dev)) if os.path.exists(dev) else os.path.basename(dev)
        if base.startswith(("dm-", "md")):
            disks, shared = _holders_disks(base), True
        else:
            disks, shared = {_parent_disk(dev)}, False
        if shared and len(disks) <= 1:
            shared = False          # 单盘 LVM：就是这块盘自己的空间
        for d in disks:
            u = out.setdefault(d, {"total": 0, "used": 0, "free": 0, "mounts": [], "shared": False})
            u["total"] += fs.get("total") or 0
            u["used"] += fs.get("used") or 0
            u["free"] += fs.get("free") or 0
            u["mounts"].append(fs.get("mount") or "")
            u["shared"] = u["shared"] or shared
    for u in out.values():
        u["pct"] = round(u["used"] * 100.0 / u["total"], 1) if u["total"] else None
    return out


def read_filesystems():
    """HWINFO.filesystems：过滤 <1GB 的小分区（/boot 类除外，由调用方决定是否保留——
    这里按 HW_SPEC 字面「<1GB 的 /boot 类小分区可保留」，即只过滤真正的伪文件系统，
    不按容量过滤，避免把用户特意看的小分区也隐藏掉。"""
    out = []
    for dev, mnt, fstype in parse_mounts(rd("/proc/mounts", "") or ""):
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        free = st.f_bavail * st.f_frsize
        out.append({"mount": mnt, "dev": dev, "fstype": fstype, "total": total, "used": used,
                    "free": free, "pct": round(used * 100.0 / total, 1)})
    return out


# ================================================================
# 软 RAID（mdraid） —— 移植自 jkmon linux_io.mdraid()
# ================================================================

def parse_mdstat(txt):
    """/proc/mdstat → [{name, level, state, members, sync_pct}, ...]。

    state 取自成员健康串 "[UU]"/"[U_]"：
      全 U         → clean
      含 _         → degraded
      没有健康串（正在 resync/recovery 等）→ 用 sync_action 或 "resync" 兜底
    其余状态原样透传（HW_SPEC: "clean | degraded | resync | ..." 原样）。
    """
    out, cur = [], None
    for line in (txt or "").splitlines():
        m = re.match(r"^(md\S+)\s*:\s*(\S+)\s+(?:\((\S+)\)\s+)?(raid\d+|linear|raid10)?\s*(.*)$", line)
        if m:
            # HW_SPEC 示例的 members 是 ["sda1","sdb1"]（带分区号），与 jkmon 原版
            # mdraid()（剥掉分区号只留整盘名）不同——这是刻意的移植偏差，按契约保留
            # 分区号：\S+? 吃掉设备名整段（含分区号），直到遇到 "[序号]"。
            members = re.findall(r"(\S+?)\[\d+\](\(F\))?", m.group(5))
            cur = {"name": m.group(1), "md_state": m.group(2), "level": m.group(4) or "",
                   "members": [x[0] for x in members], "health": None, "sync_pct": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        h = re.search(r"\[([U_]+)\]", line)
        if h:
            cur["health"] = h.group(1)
        p = re.search(r"(resync|recovery|reshape|check)\s*=\s*([\d.]+)%", line)
        if p:
            cur["sync_pct"] = float(p.group(2))
            cur["_action"] = p.group(1)
    for r in out:
        health = r.pop("health", None)
        action = r.pop("_action", None)
        md_state = r.pop("md_state", "")
        if action:
            state = action
        elif health and "_" in health:
            state = "degraded"
        elif health:
            state = "clean" if md_state in ("active", "clean") else md_state
        else:
            state = md_state
        r["state"] = state
        r["kind"] = "mdraid"
    return out


def read_mdraid():
    txt = rd("/proc/mdstat")
    if not txt:
        return []
    return parse_mdstat(txt)


# ================================================================
# 网卡 —— 移植自 jkmon linux_io.netdev() / nic_info()
# 沿用 hwctl._NET_IGNORE 的过滤口径（lo/docker/veth/br-/virbr/tun/tap）。
# ================================================================

def parse_proc_net_dev(txt):
    """/proc/net/dev → {name: (rx_bytes, tx_bytes)}，过滤掉 _NET_IGNORE 前缀的虚拟接口。"""
    out = {}
    lines = (txt or "").splitlines()
    for line in lines[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or any(name.startswith(p) for p in _NET_IGNORE):
            continue
        cols = rest.split()
        if len(cols) >= 9:
            try:
                out[name] = (int(cols[0]), int(cols[8]))
            except ValueError:
                continue
    return out


def _nic_ip_addrs():
    """ip -o addr show 的解析：{iface: [cidr, ...]}，分 ipv4/ipv6。没有 ip 命令返回空。"""
    v4, v6 = {}, {}
    exe = which("ip")
    if not exe:
        return v4, v6
    rc, out = run([exe, "-o", "addr", "show"], timeout=5)
    if rc != 0:
        return v4, v6
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[2] in ("inet", "inet6") and "scope link" not in line:
            name = f[1].rstrip(":").split("@")[0]
            if any(name.startswith(p) for p in _NET_IGNORE):
                continue
            (v4 if f[2] == "inet" else v6).setdefault(name, []).append(f[3])
    return v4, v6


def read_nics():
    """HWINFO.nics：名称/MAC/驱动/速率/双工/上下线/MTU/IPv4/IPv6。"""
    names = list(parse_proc_net_dev(rd("/proc/net/dev", "") or "").keys())
    v4, v6 = _nic_ip_addrs()
    out = []
    for n in sorted(names):
        p = "/sys/class/net/" + n
        spd = rd_int(p + "/speed")
        drv = ""
        drv_path = p + "/device/driver"
        if os.path.exists(drv_path):
            try:
                drv = os.path.basename(os.path.realpath(drv_path))
            except OSError:
                drv = ""
        duplex = rd(p + "/duplex", "") or ""
        out.append({
            "name": n,
            "mac": rd(p + "/address", "") or "",
            "driver": drv,
            "speed_mbps": spd if spd and spd > 0 else None,
            "duplex": duplex,
            "up": rd(p + "/operstate", "") == "up",
            "mtu": rd_int(p + "/mtu"),
            "ipv4": v4.get(n, []),
            "ipv6": v6.get(n, []),
        })
    return out


_NIC_RATE_PREV = {}
_NIC_RATE_LOCK = threading.Lock()


def read_nic_rates():
    """/api/sensor 用的网卡速率快照：{name, up, rx_bps, tx_bps, speed_mbps}。
    首次调用（无基线）速率为 0，与 hwctl.read_net_rate() 的「首次无基线」约定一致。
    与 hwctl._NET_PREV 分开维护自己的基线状态，互不干扰。"""
    now = time.time()
    cur = parse_proc_net_dev(rd("/proc/net/dev", "") or "")
    out = []
    with _NIC_RATE_LOCK:
        for name, (rx, tx) in sorted(cur.items()):
            prev = _NIC_RATE_PREV.get(name)
            _NIC_RATE_PREV[name] = (now, rx, tx)
            p = "/sys/class/net/" + name
            spd = rd_int(p + "/speed")
            rx_bps = tx_bps = 0
            if prev:
                dt = now - prev[0]
                if dt > 0:
                    rx_bps = max(0, int((rx - prev[1]) / dt))
                    tx_bps = max(0, int((tx - prev[2]) / dt))
            out.append({"name": name, "up": rd(p + "/operstate", "") == "up",
                        "rx_bps": rx_bps, "tx_bps": tx_bps,
                        "speed_mbps": spd if spd and spd > 0 else None})
    return out


# ================================================================
# 磁盘身份识别 —— 移植自 jkmon disk_id.brand()（型号前缀表，裁剪到常见品牌）
# 完整表有上百条正则，这里只保留 NAS 场景常见的机械盘/SSD 厂商，够用即可；
# 查不到品牌不是错误，brand 字段留空串，前端按「缺值显示—」处理。
# ================================================================

_BRANDS = (
    (r"^(SAMSUNG|MZ[0-9A-Z]{2}|MZ-)", "Samsung"),
    (r"^(WDC|WD[A-Z0-9]|WESTERN DIGITAL)", "WD"),
    (r"^(HGST|HITACHI|HUS|HUH|HUA|HTS|HDS|HTE)", "HGST"),
    (r"^(ST\d|SEAGATE)", "Seagate"),
    (r"(KIOXIA|^K[BX]G|^KXD|^KCD|^KCM|^KPM)", "KIOXIA"),
    (r"^(TOSHIBA|DT01|HDW|MG\d{2}|MQ\d{2})", "Toshiba"),
    (r"^(INTEL|SSDSC|SSDPE)", "Intel"),
    (r"^(CT\d|CRUCIAL)", "Crucial"),
    (r"^(MICRON|MTFD)", "Micron"),
    (r"^(KINGSTON|SA400|SKC|SNV|SUV|KC\d{3,4})", "Kingston"),
    (r"^(SANDISK|SDSSD)", "SanDisk"),
    (r"^(ADATA|XPG|SX\d{4}|SU\d{3})", "ADATA"),
    (r"^(VMWARE|QEMU|VBOX|VIRTUAL|MSFT|MICROSOFT|VIRTIO)", "虚拟磁盘"),
)


def disk_brand(model):
    m = re.sub(r"^(NVME|ATA|SCSI)\s+", "", (model or "").strip().upper())
    for pat, name in _BRANDS:
        if re.search(pat, m, re.I):
            return name
    return ""


# ================================================================
# 磁盘枚举 / 身份 / diskstats —— 移植自 jkmon linux_io.disks() + _ident() + _udev()
# ================================================================

_SKIP_BLK = ("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd", "zd")
# eMMC 除了数据区 mmcblkN，还会在 /sys/block 下挂出硬件分区：mmcblkNboot0/boot1（各 4MB，放 bootloader）
# 和 mmcblkNrpmb（安全存储）。它们不是盘，也装不了系统，列出来只会多出两行「4 MB 未挂载」的「硬盘」。
_MMC_HW_PART = re.compile(r"^mmcblk\d+(boot\d+|rpmb|gp\d+)$")


def is_real_disk(name):
    """/sys/block 里的名字是不是一块真正的盘（排掉 eMMC 硬件分区）。hwctl.list_disks 也用这个。"""
    return not _MMC_HW_PART.match(name or "")


# 盘位：S2 两个 3.5 寸仓分别接在两路 SATA 控制器上（跟 firmware/sata-led-manager.sh 的
# SLOT1_PATH / SLOT2_PATH 同一份对应关系）。sysfs 真实路径里出现哪个控制器地址就是哪一仓。
# 可用环境变量 S2_DISK_SLOTS="fc400000.sata=1,fc800000.sata=2" 覆盖（换板子时不用改代码）。
_DEFAULT_SLOTS = (("fc400000.sata", 1), ("fc800000.sata", 2))


def _slot_table():
    raw = os.environ.get("S2_DISK_SLOTS", "")
    if not raw:
        return _DEFAULT_SLOTS
    out = []
    for part in raw.split(","):
        k, _, v = part.partition("=")
        if k.strip() and v.strip().isdigit():
            out.append((k.strip(), int(v)))
    return tuple(out) or _DEFAULT_SLOTS


def disk_slot(name, real_path, bus):
    """→ (slot_no 或 None, 中文槽位文字)。

    两路 SATA 仓给「盘位 1 / 盘位 2」；其它接口不是仓位，如实写接口：
    M.2（NVMe）、USB、板载闪存（eMMC）。认不出就给空串，前端显示「—」。"""
    rp = real_path or ""
    for key, no in _slot_table():
        if "/" + key + "/" in rp or rp.endswith("/" + key):
            return no, "盘位 %d" % no
    if bus == "nvme":
        return None, "M.2"
    if bus == "usb":
        return None, "USB"
    if bus == "mmc":
        return None, "板载闪存"
    return None, ""


def disk_bus_kind(name, real_path):
    """sd*/nvme*/mmcblk* → bus 类型字符串（HW_SPEC: sata | nvme | usb | mmc | virtual | unknown）。"""
    if name.startswith("nvme"):
        return "nvme"
    if "/usb" in (real_path or ""):
        return "usb"
    if name.startswith("mmcblk"):
        return "mmc"
    if name.startswith("vd") or "/virtio" in (real_path or ""):
        return "virtual"
    if re.match(r"^sd[a-z]+$", name):
        return "sata"
    return "unknown"


def _vpd80(path):
    """SCSI VPD 80h（单元序列号）：4 字节头 + ASCII。"""
    raw = rd_bytes(path)
    if not raw or len(raw) <= 4:
        return None
    n = (raw[2] << 8) | raw[3]
    return raw[4:4 + n].decode("ascii", "replace").strip(" \0") or None


def _udev_attrs(block_dir):
    """udev 数据库里这块盘的属性（/run/udev/data/b主:次）；没有 udev 的系统返回 {}。"""
    mm = rd(block_dir + "/dev")
    out = {}
    if not mm:
        return out
    for p in ("/run/udev/data/b" + mm, "/dev/.udev/db/b" + mm):
        txt = rd(p)
        if txt:
            for line in txt.splitlines():
                if line.startswith("E:") and "=" in line:
                    k, v = line[2:].split("=", 1)
                    out[k] = v
            break
    return out


def disk_identity(block_dir, name, real_path, model):
    """sysfs/udev 身份信息（不需要 root）：serial/firmware/vendor(brand)。"""
    dev = block_dir + "/device"
    out = {"serial": "", "firmware": ""}
    if name.startswith("nvme"):
        out["serial"] = rd(dev + "/serial", "") or ""
        out["firmware"] = rd(dev + "/firmware_rev", "") or ""
    elif name.startswith("mmcblk"):
        out["serial"] = rd(dev + "/serial", "") or ""
        out["firmware"] = rd(dev + "/fwrev", "") or ""
    else:
        vendor = (rd(dev + "/vendor", "") or "").strip()
        out["firmware"] = rd(dev + "/rev", "") or ""
        out["serial"] = _vpd80(dev + "/vpd_pg80") or rd(dev + "/serial", "") or ""
    if not out["serial"] or not model:
        u = _udev_attrs(block_dir)
        out["serial"] = out["serial"] or u.get("ID_SCSI_SERIAL") or u.get("ID_SERIAL_SHORT") or ""
        if not model and u.get("ID_MODEL"):
            model = u["ID_MODEL"].replace("_", " ").strip()
    out["model"] = model or ""
    out["vendor"] = disk_brand(model)
    return out


def parse_diskstats(txt):
    """/proc/diskstats → {name: (rd_sectors, wr_sectors, io_ticks_ms)}。沿用 hwctl.read_diskstats 的字段下标约定。"""
    out = {}
    for line in (txt or "").splitlines():
        parts = line.split()
        if len(parts) < 13:
            continue
        name = parts[2]
        try:
            out[name] = (int(parts[5]), int(parts[9]), int(parts[12]))
        except ValueError:
            continue
    return out


def enumerate_disks():
    """/sys/block/* 里的整盘清单（排除分区/loop/dm/md 等），每项带 sysfs 可读的身份信息。"""
    out = []
    base = "/sys/block"
    if not os.path.isdir(base):
        return out
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if name.startswith(_SKIP_BLK) or not is_real_disk(name):
            continue
        b = os.path.join(base, name)
        if not os.path.exists(b + "/device"):
            continue
        real = os.path.realpath(b)
        sectors = rd_int(b + "/size") or 0
        model = rd(b + "/device/model", "") or rd(b + "/device/name", "") or ""
        model = model.strip()
        ident = disk_identity(b, name, real, model)
        rot = rd(b + "/queue/rotational", "1") == "1"
        bus = disk_bus_kind(name, real)
        slot_no, slot_txt = disk_slot(name, real, bus)
        out.append({
            "name": name, "path": "/dev/" + name, "bytes": sectors * 512,
            "bus": bus, "rotational": rot, "slot": slot_no, "slot_name": slot_txt,
            "removable": rd(b + "/removable", "0") == "1",
            "model": ident.get("model", ""), "serial": ident.get("serial", ""),
            "firmware": ident.get("firmware", ""), "vendor": ident.get("vendor", ""),
        })
    return out


# ================================================================
# SMART —— 移植自 jkmon linux_disk.py（SG_IO ATA PASS-THROUGH / NVMe admin ioctl）
#         + smart_parse.py（属性表解析）+ smart.py（smartctl -j JSON 兜底）
#
# 硬规则（HW_SPEC 第 2 节）：
#   · 机械盘读 SMART 前必须先 CHECK POWER MODE；休眠就整段跳过，不发送任何其它命令。
#   · fcntl / ctypes 的 ioctl 用法是 Linux-only，这里全部惰性 import，
#     保证本模块在 Windows 开发机上 `import hwinfo` 不报错。
#   · 需要 root（CAP_SYS_RAWIO）；非 root 时 _open() 多半拿到权限错误，
#     上层 collect_hwinfo() 据此落 smart_err="需要 root"。
# ================================================================

def ok_temp(t):
    return t is not None and 0 < t < 150


def checksum_ok(data):
    return bool(data) and len(data) >= 512 and any(data[:362]) and sum(bytearray(data[:512])) % 256 == 0


def _sg_open(dev):
    try:
        return os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None


def _sg_io(fd, cdb, n, timeout_ms=10000):
    """发一条 SCSI/ATA-passthrough CDB，返回 (数据, 感知数据, SCSI状态)；失败返回 (None, None, None)。
    ctypes/fcntl 在函数内部导入——Windows 开发机 import 本模块不会报错，只有真的调用到
    这个函数（即在 Linux 上、且拿到了 fd）才会用到。"""
    import ctypes
    import fcntl

    class _SgHdr(ctypes.Structure):
        _fields_ = [("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
                    ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
                    ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
                    ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p), ("sbp", ctypes.c_void_p),
                    ("timeout", ctypes.c_uint), ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
                    ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte), ("masked_status", ctypes.c_ubyte),
                    ("msg_status", ctypes.c_ubyte), ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
                    ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int), ("duration", ctypes.c_uint),
                    ("info", ctypes.c_uint)]

    SG_IO = 0x2285
    FROM_DEV, NONE_DIR = -3, -1
    buf = ctypes.create_string_buffer(max(1, n))
    sense = ctypes.create_string_buffer(64)
    cmd = ctypes.create_string_buffer(bytes(cdb), len(cdb))
    h = _SgHdr(interface_id=ord("S"), dxfer_direction=FROM_DEV if n else NONE_DIR, cmd_len=len(cdb),
               mx_sb_len=64, dxfer_len=n, dxferp=ctypes.addressof(buf) if n else None,
               cmdp=ctypes.addressof(cmd), sbp=ctypes.addressof(sense), timeout=timeout_ms)
    try:
        fcntl.ioctl(fd, SG_IO, h)
    except OSError:
        return None, None, None
    if h.host_status or (h.driver_status & 0x0F and h.driver_status & 0x0F != 0x08):
        return None, None, None
    return buf.raw[:n - max(0, h.resid)] if n else b"", sense.raw[:h.sb_len_wr], h.status


def _ata_regs(sense):
    """从感知数据取出 ATA 返回寄存器 (错误, 扇区数, LBA低, LBA中, LBA高, 设备, 状态)。"""
    if not sense or len(sense) < 8:
        return None
    code = sense[0] & 0x7F
    if code in (0x72, 0x73):
        i, end = 8, 8 + sense[7]
        while i + 1 < min(end, len(sense)):
            if sense[i] == 0x09 and i + 13 < len(sense):
                d = sense[i:i + 14]
                return d[3], d[5], d[7], d[9], d[11], d[12], d[13]
            i += 2 + sense[i + 1]
        return None
    if code in (0x70, 0x71) and len(sense) >= 18:
        return sense[3], sense[6], sense[9], sense[10], sense[11], sense[5], sense[4]
    return None


def _ata_pt16(fd, cmd, feat=0, count=0, lba=0, n=0, ck=False, timeout_ms=10000):
    """ATA PASS-THROUGH(16)：PIO 读（protocol 4）或无数据（protocol 3）。"""
    proto = 4 if n else 3
    b2 = (0x20 if ck else 0) | ((0x08 | 0x04 | 0x02) if n else 0)
    cdb = bytes([0x85, proto << 1, b2, 0, feat, 0, count, 0, lba & 0xFF, 0, (lba >> 8) & 0xFF, 0,
                 (lba >> 16) & 0xFF, 0xA0 if cmd == 0xB0 else 0, cmd, 0])
    data, sense, st = _sg_io(fd, cdb, 512 * n, timeout_ms)
    if data is None:
        return None, None
    regs = _ata_regs(sense)
    if regs and regs[6] & 0x01:
        return None, regs
    if st not in (0, 2):
        return None, regs
    if st == 2:
        fixed = bool(sense) and (sense[0] & 0x7F) in (0x70, 0x71)
        key = (sense[2] if fixed and len(sense) > 2 else sense[1] if sense and len(sense) > 1 else 0xFF) & 0x0F
        if key not in (0, 1):
            return None, regs
    return data, regs


def _ata_smart_read(fd, feat, n=1, ck=False):
    return _ata_pt16(fd, 0xB0, feat=feat, count=1 if n else 0, lba=0x4F << 8 | 0xC2 << 16, n=n, ck=ck)


def ata_check_power_mode(fd):
    """CHECK POWER MODE（E5h）：返回 'standby' / 'active' / None（桥接芯片不返回寄存器时，无法判定）。"""
    _, regs = _ata_pt16(fd, 0xE5, ck=True)
    if not regs:
        return None
    return "standby" if regs[1] == 0x00 else "active"


def _nvme_admin(fd, opcode, nsid=0, cdw10=0, cdw11=0, n=0, timeout_ms=5000):
    import ctypes
    import fcntl

    class _NvmeCmd(ctypes.Structure):
        _fields_ = [("opcode", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("rsvd1", ctypes.c_ushort),
                    ("nsid", ctypes.c_uint), ("cdw2", ctypes.c_uint), ("cdw3", ctypes.c_uint),
                    ("metadata", ctypes.c_ulonglong), ("addr", ctypes.c_ulonglong),
                    ("metadata_len", ctypes.c_uint), ("data_len", ctypes.c_uint),
                    ("cdw10", ctypes.c_uint), ("cdw11", ctypes.c_uint), ("cdw12", ctypes.c_uint),
                    ("cdw13", ctypes.c_uint), ("cdw14", ctypes.c_uint), ("cdw15", ctypes.c_uint),
                    ("timeout_ms", ctypes.c_uint), ("result", ctypes.c_uint)]

    NVME_ADMIN = 0xC0484E41
    buf = ctypes.create_string_buffer(max(1, n))
    c = _NvmeCmd(opcode=opcode, nsid=nsid, addr=ctypes.addressof(buf) if n else 0, data_len=n,
                 cdw10=cdw10, cdw11=cdw11, timeout_ms=timeout_ms)
    try:
        rc = fcntl.ioctl(fd, NVME_ADMIN, c)
    except OSError:
        return None
    return buf.raw[:n] if rc == 0 else None


def parse_ata_smart_attrs(data):
    """ATA SMART 属性表（512 字节）→ [(id, flags, current, worst, raw6bytes), ...]。纯函数，便于单测。"""
    out = []
    if not data or len(data) < 362:
        return out
    for i in range(30):
        o = 2 + 12 * i
        if o + 11 > len(data):
            break
        aid, flags, cur, worst = struct.unpack_from("<BHBB", data, o)
        if aid:
            out.append((aid, flags, cur, worst, bytearray(data[o + 5:o + 11])))
    return out


def _raw48(r):
    return int.from_bytes(bytes(r[:6]), "little")


def ata_smart_to_dict(data, thresholds, passed):
    """ATA SMART 属性表 + 阈值表 → HWINFO.disks[].smart 的核心字段。"""
    attrs = parse_ata_smart_attrs(data)
    th = {}
    if thresholds:
        for i in range(30):
            o = 2 + 12 * i
            if o + 1 < len(thresholds):
                th[thresholds[o]] = thresholds[o + 1]
    temp = hours = None
    reallocated = pending = uncorrectable = 0
    wear_pct = None
    health = "ok"
    for aid, flags, cur, worst, raw in attrs:
        v = _raw48(raw)
        if aid in (194, 190) and temp is None and ok_temp(raw[0]):
            temp = int(raw[0])
        if aid == 9 and hours is None:
            hours = v & 0xFFFFFFFF
        if aid == 5:
            reallocated = v
        elif aid == 197:
            pending = v
        elif aid == 198:
            uncorrectable = v
        if aid in (231, 169, 233, 177, 173, 202) and 0 < cur <= 100:
            wear_pct = 100 - cur
        t = th.get(aid)
        if t and flags & 1 and cur <= t:
            health = "fail"
    if passed is False:
        health = "fail"
    elif health == "ok" and (reallocated or pending or uncorrectable):
        health = "warn"
    return {"health": health, "temp": temp, "power_on_hours": hours, "power_cycles": None,
            "reallocated": reallocated, "pending": pending, "uncorrectable": uncorrectable,
            "wear_pct": wear_pct, "written_bytes": None}


def parse_nvme_smart_log(log):
    """NVMe SMART / Health Information Log（512 字节）→ HWINFO.disks[].smart 核心字段。"""
    if not log or len(log) < 64:
        return None
    cw, kel, spare, spare_t, used = struct.unpack_from("<BHBBB", log, 0)

    def u128(o):
        return int.from_bytes(bytes(log[o:o + 16]), "little") if o + 16 <= len(log) else None

    temp_c = int(kel - 273) if kel and ok_temp(kel - 273) else None
    health = "ok"
    if cw:
        health = "fail"
    elif used >= 90:
        health = "warn"
    written = u128(48)
    written_bytes = written * 512000 if written is not None else None
    hours = u128(128)
    power_cycles = u128(112)
    return {"health": health, "temp": temp_c, "power_on_hours": hours, "power_cycles": power_cycles,
            "reallocated": 0, "pending": 0, "uncorrectable": u128(160) or 0,
            "wear_pct": used, "written_bytes": written_bytes}


def smart_read_native(dev_path, name, rotational):
    """返回 (smart_dict_or_None, standby_bool, err_str)。

    机械盘（ATA + rotational）先 CHECK POWER MODE；standby 直接返回不读任何其它命令。
    NVMe 不存在「休眠」概念（这里不对 NVMe 盘做电源状态判定），直接读健康日志。
    桥接芯片 / 权限不足 / 读取失败时 smart=None，err 给出原因；调用方据此决定是否
    再退到 smartctl JSON。
    """
    fd = _sg_open(dev_path)
    if fd is None:
        return None, False, "需要管理员权限"
    try:
        is_nvme = name.startswith("nvme")
        if is_nvme:
            log = _nvme_admin(fd, 0x02, nsid=0xFFFFFFFF, cdw10=0x02 | ((512 // 4 - 1) << 16), n=512)
            if not log:
                return None, False, "NVMe 健康日志读取失败"
            r = parse_nvme_smart_log(log)
            return r, False, "" if r else "NVMe 健康日志解析失败"
        # ATA：先查电源状态，机械盘休眠就立刻止步
        if rotational:
            pm = ata_check_power_mode(fd)
            if pm == "standby":
                return None, True, "盘在休眠，未唤醒"
            if pm is None:
                return None, False, "未安装 smartctl 且原生接口不支持"
        data, _ = _ata_smart_read(fd, 0xD0)
        if not checksum_ok(data):
            return None, False, "未安装 smartctl 且原生接口不支持"
        thr, _ = _ata_smart_read(fd, 0xD1)
        _, regs = _ata_smart_read(fd, 0xDA, n=0, ck=True)
        passed = None
        if regs:
            if (regs[3], regs[4]) == (0xF4, 0x2C):
                passed = False
            elif (regs[3], regs[4]) == (0x4F, 0xC2):
                passed = True
        r = ata_smart_to_dict(data, thr if checksum_ok(thr) else None, passed)
        return r, False, ""
    finally:
        os.close(fd)


def smartctl_json(dev_path):
    """Explicit device type and fail-closed power handling shared with fan collection."""
    d, state = disk_reader.read_smart(dev_path, full=True, runner=run, find=which)
    if d is None:
        return None, state == "standby"
    smart_status = d.get("smart_status") or {}
    passed = smart_status.get("passed")
    health = "ok" if passed is not False else "fail"
    nvme = d.get("nvme_smart_health_information_log")
    if nvme:
        cw = nvme.get("critical_warning") or 0
        used = nvme.get("percentage_used") or 0
        if cw:
            health = "fail"
        elif used >= 90:
            health = "warn"
        written = nvme.get("data_units_written")
        return {"health": health, "temp": (d.get("temperature") or {}).get("current"),
                "power_on_hours": (d.get("power_on_time") or {}).get("hours"),
                "power_cycles": nvme.get("power_cycles"), "reallocated": 0, "pending": 0,
                "uncorrectable": nvme.get("media_errors") or 0, "wear_pct": used,
                "written_bytes": written * 512000 if written is not None else None}, False
    reallocated = pending = uncorrectable = 0
    for a in (d.get("ata_smart_attributes") or {}).get("table", []):
        raw = (a.get("raw") or {}).get("value")
        if a.get("id") == 5 and isinstance(raw, int):
            reallocated = raw
        elif a.get("id") == 197 and isinstance(raw, int):
            pending = raw
        elif a.get("id") == 198 and isinstance(raw, int):
            uncorrectable = raw
        if a.get("when_failed"):
            health = "fail"
    if health == "ok" and (reallocated or pending or uncorrectable):
        health = "warn"
    return {"health": health, "temp": (d.get("temperature") or {}).get("current"),
            "power_on_hours": (d.get("power_on_time") or {}).get("hours"),
            "power_cycles": d.get("power_cycle_count"), "reallocated": reallocated,
            "pending": pending, "uncorrectable": uncorrectable, "wear_pct": None,
            "written_bytes": None}, False


def disk_is_standby(dev_path, name, rotational):
    """只读电源状态，不读任何会唤醒盘的命令。非机械盘（SSD/NVMe/eMMC）恒为 False。
    用于 /api/sensor（disk_list[].standby）：不能调用 smartctl（不符合「/api/sensor 不起子进程」
    的硬规则），只用原生 SG_IO CHECK POWER MODE；读不到（非 root / 桥接芯片）返回 None。"""
    if name.startswith("nvme") or not rotational:
        return False
    fd = _sg_open(dev_path)
    if fd is None:
        return None
    try:
        pm = ata_check_power_mode(fd)
        if pm is None:
            return None
        return pm == "standby"
    finally:
        os.close(fd)


def _infer_disk_role(name, filesystems, is_root):
    """按挂载点推断「系统盘 / 存储空间 N」。启发式，推断不出给空串（HW_SPEC 允许）：
      - 该盘任一分区挂载在 / 上 → 系统盘
      - 其余按首次出现顺序编号为 存储空间 1/2/3...
    这段在真实 fnOS 挂载布局上未经验证，是本次移植里置信度最低的一处——
    fnOS 的卷命名/挂载层级（是否有 /vol1 这种命名，是否整盘直接挂载还是走 LVM/btrfs 子卷）
    都没有在目标机上实测过，参见报告里的「未验证假设」。
    """
    root_disks = set()
    data_disks = []
    for fs in filesystems:
        disk = fs.get("_disk") or ""
        if not disk:
            continue
        if fs.get("mount") == "/":
            root_disks.add(disk)
        elif disk not in data_disks:
            data_disks.append(disk)
    if name in root_disks:
        return "系统盘"
    if name in data_disks:
        return "存储空间 %d" % (data_disks.index(name) + 1)
    return ""


def read_disks(include_smart, is_root):
    """HWINFO.disks：枚举 + diskstats 关联 + （可选）SMART。

    include_smart=False 时（非 root 本地只读兜底路径）直接跳过所有 SMART/电源状态
    探测，只给 smart=None + smart_err="需要管理员权限"——避免在无 root 的情况下还去
    尝试打开块设备触碰权限错误路径。
    """
    disks = enumerate_disks()
    stats = parse_diskstats(rd("/proc/diskstats", "") or "")
    mounts = parse_mounts(rd("/proc/mounts", "") or "")
    mount_by_disk = {}
    for dev, mnt, _fstype in mounts:
        disk_name = _parent_disk(dev)
        mount_by_disk.setdefault(disk_name, []).append(mnt)

    fs_list = []
    for dev, mnt, fstype in mounts:
        fs_list.append({"mount": mnt, "_disk": _parent_disk(dev)})
    # statvfs 只在这里做一次（每个挂载点一次系统调用，不起进程），按盘汇总已用 / 空闲
    try:
        usage = disk_usage_by_disk(read_filesystems())
    except Exception:
        usage = {}

    out = []
    for d in disks:
        name = d["name"]
        smart = None
        smart_err = ""
        standby = False
        if d.get("bus") == "mmc" or name.startswith("mmcblk"):
            smart_err = "闪存不支持健康检测"   # eMMC：不发 SG_IO、不起 smartctl，省两次必然失败的尝试
        elif not include_smart:
            smart_err = "需要管理员权限"
        else:
            # 每块盘单独兜底：一块盘的 ioctl/解析出错只让这块盘 smart=None，不能让整张盘表变空。
            try:
                standby = disk_is_standby(d["path"], name, d["rotational"]) or False
                if standby:
                    smart_err = "盘在休眠，未唤醒"
                else:
                    # smart_read_native 返回三元组 (smart, standby, err)
                    smart, sb, serr = smart_read_native(d["path"], name, d["rotational"])
                    if sb:
                        standby, smart_err = True, "盘在休眠，未唤醒"
                    elif smart is None and serr != "需要管理员权限":
                        alt, alt_standby = smartctl_json(d["path"])
                        if alt_standby:
                            standby, smart_err = True, "盘在休眠，未唤醒"
                        elif alt is not None:
                            smart = alt
                        else:
                            smart_err = serr or "未安装 smartctl 且原生接口不支持"
                    elif smart is None:
                        smart_err = serr
            except Exception:
                smart, smart_err = None, "读取健康数据出错"
        if smart is not None:
            smart["ts"] = int(time.time())
        rpm = None  # 转速需要 ATA IDENTIFY word 217，当前只在 SMART 原生路径读取过 IDENTIFY 时才有；
                    # smartctl JSON 兜底路径没有解析 IDENTIFY，这里统一先置空，不去猜。
        out.append({
            "name": name, "path": d["path"], "model": d["model"], "serial": d["serial"],
            "firmware": d["firmware"], "vendor": d["vendor"], "bytes": d["bytes"],
            "bus": d["bus"], "rotational": d["rotational"], "removable": d["removable"],
            "rpm": rpm, "smart": smart, "smart_err": smart_err, "standby": standby,
            "mounts": mount_by_disk.get(name, []),
            "role": _infer_disk_role(name, fs_list, is_root),
            "slot": d.get("slot"), "slot_name": d.get("slot_name", ""),
            # 已挂载容量：没挂载（新盘 / 只做了阵列成员还没建卷）就是 None，前端写「未挂载」
            "usage": usage.get(name),
        })
    return out


# ================================================================
# 缓存 + 顶层入口
# ================================================================

_CACHE_LOCK = threading.Lock()
_CACHE = {"base": None, "base_ts": 0.0, "smart": None, "smart_ts": 0.0}
BASE_TTL = 60
SMART_TTL = 600
FORCE_MIN_GAP = 15


def _is_root():
    fn = getattr(os, "geteuid", None)
    return bool(fn) and fn() == 0


def collect_hwinfo(force=False):
    """HWINFO 顶层入口：system/cpu/memory/disks/filesystems/raids/nics。

    缓存策略（HW_SPEC 第 1 节）：非 SMART 部分 60 秒，SMART 部分 600 秒，各自独立
    过期，用一把锁保护（ThreadingHTTPServer 多线程同时轮询 /api/hwinfo 时避免
    并发起多份子进程/ioctl）。force=True 强制重采两部分，但休眠盘依然不唤醒——
    这是数据新鲜度和安全规则的边界：force 只是跳过缓存，不会改变「不碰休眠盘」
    这条底线。
    """
    now = time.time()
    root = _is_root()
    with _CACHE_LOCK:
        # force 也有下限：连点刷新 / 局域网里有人刷 ?force=1，最多每 FORCE_MIN_GAP 秒真采一次，
        # 不会每次都对每块盘发 SG_IO、起 smartctl。
        f_base = force and now - _CACHE["base_ts"] >= FORCE_MIN_GAP
        f_smart = force and now - _CACHE["smart_ts"] >= FORCE_MIN_GAP
        need_base = f_base or _CACHE["base"] is None or now - _CACHE["base_ts"] >= BASE_TTL
        need_smart = f_smart or _CACHE["smart"] is None or now - _CACHE["smart_ts"] >= SMART_TTL
        base, smart_disks = _CACHE["base"], _CACHE["smart"]

    # 两类采集都可能有 IO/子进程开销，不在持锁状态下做，避免互相阻塞；
    # 缓存字典的读写各自加锁，中间这段「没持锁」的窗口期最坏情况是重复采集一次，
    # 比持锁跨子进程调用安全得多。
    if need_base:
        try:
            system = system_static()
        except Exception:
            system = {"hostname": "", "board": "", "os": "", "kernel": "", "arch": "", "boot_ts": None}
        try:
            cpu = cpu_static()
        except Exception:
            cpu = {"model": None, "core_name": None, "vendor": None, "arch": "", "cores": None,
                   "threads": None, "max_mhz": None, "min_mhz": None, "governor": "", "cache": "", "flags": []}
        try:
            memory = memory_static()
        except Exception:
            memory = {"total": 0, "swap_total": 0, "dimms": []}
        try:
            filesystems = read_filesystems()
        except Exception:
            filesystems = []
        try:
            raids = read_mdraid()
        except Exception:
            raids = []
        try:
            nics = read_nics()
        except Exception:
            nics = []
        base = {"system": system, "cpu": cpu, "memory": memory,
                "filesystems": filesystems, "raids": raids, "nics": nics}
        with _CACHE_LOCK:
            _CACHE["base"], _CACHE["base_ts"] = base, now

    if need_smart:
        try:
            smart_disks = read_disks(include_smart=root, is_root=root)
        except Exception:
            smart_disks = []
        with _CACHE_LOCK:
            _CACHE["smart"], _CACHE["smart_ts"] = smart_disks, now

    # ts 表示「这份数据实际的采集时刻」，不是「这次调用的时刻」：缓存命中时
    # 两部分可能来自不同时间点的采集，取较晚（更新）的那个 base_ts/smart_ts，
    # 让调用方能看出数据有多旧，而不是每次调用都看到一个假装刚采集的新 ts。
    with _CACHE_LOCK:
        ts = max(_CACHE["base_ts"] or 0, _CACHE["smart_ts"] or 0) or now

    out = {"ts": int(ts), "root": root}
    out.update(base or {})
    out["disks"] = smart_disks or []
    return out
