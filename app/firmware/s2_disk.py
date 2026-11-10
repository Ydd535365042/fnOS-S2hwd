#!/usr/bin/env python3
"""Shared disk reads: no drivetemp, no device autodetection, unknown power fails closed."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def device_type(device):
    name = os.path.basename(device)
    if device != "/dev/" + name:
        return None
    if re.fullmatch(r"nvme\d+n\d+", name):
        return "nvme"
    if not re.fullmatch(r"sd[a-z]+", name):
        return None
    path = os.path.realpath("/sys/class/block/" + name + "/device").replace("\\", "/")
    # USB bridges may need proprietary -d options; never probe them automatically.
    if re.search(r"/usb\d+(/|$)", path):
        return None
    return "ata" if re.search(r"/ata\d+/", path) else None


def run(argv, timeout):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def read_smart(device, full=False, runner=run, find=shutil.which, timeout=8):
    kind, executable = device_type(device), find("smartctl")
    if kind is None or not executable:
        return None, "unknown"
    args = [executable, "-j", "-a" if full else "-A", "-d", kind]
    if kind != "nvme":
        args += ["-n", "standby,3,5"]
    rc, output = runner(args + [device], timeout=timeout if not full else 20)
    try:
        data = json.loads(output)
        if not isinstance(data, dict): raise ValueError()
    except (ValueError, TypeError):
        return None, "unknown"
    power = str(data.get("power_mode", "")).upper()
    messages = json.dumps(data.get("smartctl", {}).get("messages", "")).upper()
    if kind != "nvme" and rc == 3 and (power in ("STANDBY", "SLEEP") or "STANDBY" in messages or "SLEEP" in messages):
        return None, "standby"
    # Bits 0..2 are command/open/parse errors. Do not turn unknown into healthy data.
    if rc is None or rc & 7 or not any(k in data for k in ("ata_smart_attributes", "nvme_smart_health_information_log", "temperature")):
        return None, "unknown"
    return data, "active"


def temperatures(timeout=8):
    for path in sorted(Path("/sys/class/block").glob("*")):
        if not re.fullmatch(r"sd[a-z]+|nvme\d+n\d+", path.name):
            continue
        data, state = read_smart("/dev/" + path.name, timeout=timeout)
        value = (data.get("temperature") or {}).get("current") if data else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 120:
            yield path.name, str(int(value))
        else:
            yield path.name, "standby" if state == "standby" else "unknown"


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".s2-disk-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def disable_drivetemp(root=Path("/")):
    """Idempotent install/upgrade/selfheal migration; preserve the first backup."""
    conf = root / "etc/modules-load.d/drivetemp.conf"
    if conf.is_symlink():
        raise OSError("拒绝覆盖符号链接 drivetemp.conf")
    previous = conf.read_text(encoding="utf-8") if conf.exists() else ""
    lines = [line for line in previous.splitlines() if line.split("#", 1)[0].strip() != "drivetemp"]
    content = "\n".join(lines) + "\n"
    if not previous:
        content = "# S2: drivetemp disabled; disk temperatures use guarded SMART reads.\n"
    blacklist = root / "etc/modprobe.d/s2-no-drivetemp.conf"
    atomic_text(blacklist, "# S2 disk standby protection\nblacklist drivetemp\ninstall drivetemp /bin/false\n")
    if content != previous:
        if previous:
            try:
                with conf.with_suffix(conf.suffix + ".s2-before-sleep.bak").open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(previous)
            except FileExistsError:
                pass
        atomic_text(conf, content)
    if (root / "sys/module/drivetemp").exists():
        rc, _ = run(["modprobe", "-r", "drivetemp"], timeout=10)
        if rc != 0 or (root / "sys/module/drivetemp").exists():
            raise OSError("drivetemp 仍被占用；已阻止下次加载，请重启后复查")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["disable-drivetemp"]:
            disable_drivetemp()
            print("硬盘休眠迁移完成：已禁用 drivetemp")
        elif len(sys.argv) in (2, 3) and sys.argv[1] == "temperatures":
            timeout = int(sys.argv[2]) if len(sys.argv) == 3 else 8
            if not 1 <= timeout <= 60: raise ValueError("温度采集超时需为 1–60 秒")
            print(" ".join(name + "=" + value for name, value in temperatures(timeout)))
        else:
            raise ValueError("usage: s2_disk.py temperatures|disable-drivetemp")
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
