"""Explicit, pinned S2 DTB installation and narrow boot-file guard. Never reboots."""
import hashlib
import contextlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import subprocess
import time
import uuid

NAME = "rk3568-witxiaohui-s2.dtb"
PIN = "rockchip/" + NAME
SHA256 = "6dc95aac914dfe5618f09027d31190ae0b84a7efdcc797fa0fa39ecfbbeeb509"
OLD_SHA256 = "38943953170941897b8dcbf41f3018fcd5fb13bb6c13e564348811ba60da5d5c"
MODEL = "Rockchip RK3568 WITXIAOHUI S2 Board NAS"
TARGETS = ["boot/dtb/rockchip/" + NAME, "boot/dtb/rockchip/rk3568-nanopi-r5s.dtb", "boot/fnEnv.txt"]
_LOCK = threading.RLock()


@contextlib.contextmanager
def locked(root):
    with _LOCK:
        path = Path(root)/"var/lib/fnos-s2/dtb.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink(): raise ValueError("DTB 锁文件不能是符号链接")
        with path.open("a+b") as stream:
            if os.name == "posix":
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield


def properties(data):
    if len(data) < 40: raise ValueError("DTB 头部缺失")
    magic, total, offset, strings, reserve, version, compatible, _, ssize, psize = struct.unpack_from(">10I", data)
    if magic != 0xd00dfeed or total != len(data) or version < 16 or compatible > 17:
        raise ValueError("DTB 头部或版本无效")
    if any(x < 40 or x >= total for x in (offset, strings, reserve)) or offset+psize > total or strings+ssize > total:
        raise ValueError("DTB 区域越界")
    names = data[strings:strings+ssize]; end = offset+psize; stack = []; result = {}; pos = offset
    while pos+4 <= end:
        token = struct.unpack_from(">I", data, pos)[0]; pos += 4
        if token == 1:
            zero = data.index(0, pos, end); stack.append(data[pos:zero].decode("ascii")); pos = (zero+4)&~3
        elif token == 2:
            if not stack: raise ValueError("DTB 节点不平衡")
            stack.pop()
        elif token == 3:
            if pos+8 > end or not stack: raise ValueError("DTB 属性无效")
            size, name = struct.unpack_from(">II", data, pos); pos += 8
            if pos+size > end or name >= len(names): raise ValueError("DTB 属性越界")
            key = names[name:names.index(0, name)].decode("ascii")
            result[("/".join(stack) or "/", key)] = data[pos:pos+size]; pos = (pos+size+3)&~3
        elif token == 4: continue
        elif token == 9:
            if stack: raise ValueError("DTB 节点未闭合")
            return result
        else: raise ValueError("未知 DTB token")
    raise ValueError("DTB 未结束")


def inspect(data):
    props = properties(data)
    model = props.get(("/", "model"), b"").decode("utf-8").rstrip("\0")
    compatibles = props.get(("/", "compatible"), b"").decode("utf-8").rstrip("\0").split("\0")
    fans = [node for (node, key), value in props.items() if key == "compatible" and b"pwm-fan\0" in value]
    levels = [node for node, key in props if key == "cooling-levels"]
    maps = [node for (node, key) in props if key == "cooling-device" and "/cpu-thermal/" in node]
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "model": model,
            "compatible": compatibles, "pwm_fan": fans, "cooling_levels": levels, "cpu_cooling_maps": maps}


def validate(data):
    report = inspect(data)
    if report["sha256"] != SHA256 or report["model"] != MODEL or not report["pwm_fan"] or not report["cooling_levels"] or not report["cpu_cooling_maps"]:
        raise ValueError("不是已核验的 S2 1.1.2 DTB，拒绝修改引导")
    return report


def atomic(path, data, mode=None):
    path = Path(path)
    if path.is_symlink(): raise ValueError("拒绝覆盖引导符号链接：" + str(path))
    mode = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fnos-s2-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read_regular(path, optional=False):
    if path.is_symlink(): raise ValueError("不接受符号链接："+str(path))
    if optional and not path.exists(): return None
    if not path.is_file(): raise ValueError("不是普通文件："+str(path))
    return path.read_bytes()


def selection(env):
    values = re.findall(rb"(?m)^[ \t]*fdtfile[ \t]*=[ \t]*([^\r\n]*)", env)
    if len(values) != 1: raise ValueError("必须有且只有一个明确的 fdtfile")
    value = values[0].decode().strip()
    if value[:1] in ("'", '"') and value[-1:] == value[:1]: value = value[1:-1]
    if not re.fullmatch(r"rockchip/[A-Za-z0-9_.-]+\.dtb", value): raise ValueError("fdtfile 路径无效")
    return value


def boot_target(root, relative):
    path = root/relative
    if not path.parent.resolve().is_relative_to((root/"boot").resolve()):
        raise ValueError("引导目标目录越界")
    return path


def environment(data):
    text = data.decode("utf-8")
    lines, replaced = [], False
    newline = "\r\n" if "\r\n" in text else "\n"
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*fdtfile\s*=", line):
            if not replaced: lines.append("fdtfile="+PIN+newline)
            replaced = True
        else: lines.append(line)
    if not replaced:
        if lines and not lines[-1].endswith(("\r", "\n")): lines[-1] += newline
        lines.append("fdtfile="+PIN+newline)
    return "".join(lines).encode()


def board_check(root, env):
    model = running_property(root, "model").decode().rstrip("\0")
    compatible = running_property(root, "compatible")
    if b"rockchip,rk3568\0" not in compatible: raise ValueError("当前设备不是 RK3568，拒绝写 DTB")
    if model == MODEL: return
    if model != "Rockchip RK3568 EVB8 LP4 V10 Board NAS":
        raise ValueError("运行中的板型不是已知的新旧 S2 配置")
    path = selection(env)
    current = root/"boot/dtb"/path
    if not current.resolve().is_relative_to((root/"boot/dtb").resolve()): raise ValueError("DTB 指向引导目录之外")
    if hashlib.sha256(read_regular(current)).hexdigest() not in (OLD_SHA256, SHA256):
        raise ValueError("当前 DTB 不是项目已知的旧 S2 版本，请先核实板型与引导配置")


def _install(candidate, root="/", writer=atomic, check_board=True):
    root = Path(root)
    for name in TARGETS: boot_target(root, name)
    data = read_regular(Path(candidate)); report = validate(data)
    env = read_regular(root/TARGETS[2])
    if check_board: board_check(root, env)
    desired = {TARGETS[0]: data, TARGETS[1]: data, TARGETS[2]: environment(env)}
    previous = {name: read_regular(root/name, optional=True) for name in TARGETS}
    reference = root/"var/lib/fnos-s2/dtb.ref"
    if previous == desired:
        if read_regular(reference, optional=True) != data: writer(reference, data)
        running = running_property(root, "model")
        return {**report, "changed": False, "backup": "", "reboot_required": running.rstrip(b"\0").decode() != MODEL}
    backup = root/"var/backups"/("fnos-s2-dtb-"+time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    manifest = {}
    for i, (name, content) in enumerate(previous.items()):
        saved = f"{i}.bin" if content is not None else None
        if saved: (backup/saved).write_bytes(content)
        manifest[name] = {"file": saved, "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
                          "mode": (root/name).stat().st_mode & 0o777 if content is not None else None}
    (backup/"manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    changed = []
    try:
        for name in TARGETS:  # DTBs first, fnEnv last; never point at a missing/partial DTB.
            if previous[name] == desired[name]: continue
            changed.append(name); writer(root/name, desired[name])
        if any(read_regular(root/name) != desired[name] for name in TARGETS): raise OSError("引导文件回读校验失败")
        writer(reference, data)
    except (OSError, ValueError):
        for name in reversed(changed):
            if previous[name] is None: (root/name).unlink(missing_ok=True)
            else: atomic(root/name, previous[name])
        raise
    return {**report, "changed": True, "backup": str(backup), "reboot_required": True}


def install(candidate, root="/", writer=atomic, check_board=True):
    with locked(root):
        return _install(candidate, root, writer, check_board)


def guard(root="/"):
    root = Path(root)
    foreign = external_reference(root)
    if foreign["present"] and not foreign["verified"]:
        raise ValueError("存在不同的独立自愈参考，停止自动覆盖以避免争用")
    if not state(root)["prepared"]:
        ledger = root/"var/lib/fnos-s2/repair-attempts.json"
        try: attempts = json.loads(read_regular(ledger, optional=True) or b"[]")
        except ValueError: attempts = []
        now = time.time()
        attempts = [t for t in attempts if type(t) in (int, float) and 0 <= now-t < 600] if isinstance(attempts, list) else []
        if len(attempts) >= 5:
            if root.resolve() == Path("/").resolve() and os.name == "posix":
                try: subprocess.run(["systemctl", "stop", "fnos-s2-dtb-guard.path"], capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError): pass
            raise ValueError("10 分钟内引导反复被改写，已暂停自动覆盖；请先排除其他写入者")
        atomic(ledger, json.dumps(attempts+[now]).encode(), mode=0o600)
    return install(root/"var/lib/fnos-s2/dtb.ref", root, check_board=False)


def running_property(root, key):
    for base in ("proc/device-tree", "sys/firmware/devicetree/base"):
        data = read_regular(Path(root)/base/key, optional=True)
        if data: return data
    return b""


def external_reference(root="/"):
    root = Path(root)
    reference = root/"var/lib/s2-dtb-guard/dtb.ref"
    if not reference.exists(): return {"present": False, "verified": False}
    data = read_regular(reference)
    config = read_regular(root/"etc/s2-dtb-guard.conf", optional=True) or b""
    pin = re.search(rb"(?m)^PIN_FDTFILE=([^\r\n]+)", config)
    return {"present": True, "verified": hashlib.sha256(data).hexdigest() == SHA256 and bool(pin) and pin[1].decode().strip() == PIN}


def _rollback(backup, root="/", writer=atomic):
    root, backup = Path(root), Path(backup)
    if not backup.resolve().is_relative_to((root/"var/backups").resolve()) or not backup.name.startswith("fnos-s2-dtb-"):
        raise ValueError("备份不在本工具的备份目录")
    manifest = json.loads(read_regular(backup/"manifest.json"))
    if not isinstance(manifest, dict) or set(manifest) != set(TARGETS): raise ValueError("备份清单无效")
    previous = {}
    modes = {}
    for i, name in enumerate(TARGETS):
        boot_target(root, name)
        item = manifest[name]
        if not isinstance(item, dict) or "file" not in item: raise ValueError("备份条目无效")
        if item["file"] is None:
            if name == "boot/fnEnv.txt": raise ValueError("备份缺少 fnEnv，拒绝回滚")
            previous[name] = None; continue
        if item["file"] != f"{i}.bin": raise ValueError("备份文件名无效")
        content = read_regular(backup/item["file"])
        if hashlib.sha256(content).hexdigest() != item["sha256"]: raise ValueError("备份校验失败")
        previous[name] = content
        modes[name] = item.get("mode", 0o644)
        if type(modes[name]) is not int or not 0 <= modes[name] <= 0o777: raise ValueError("备份权限无效")
    selected = "boot/dtb/"+selection(previous["boot/fnEnv.txt"])
    destination = boot_target(root, selected)
    data = previous[selected] if selected in previous else read_regular(destination)
    if data is None or hashlib.sha256(data).hexdigest() not in (OLD_SHA256, SHA256):
        raise ValueError("备份中的引导目标缺失或不是已核验的 S2 DTB")
    reference = root/"var/lib/fnos-s2/dtb.ref"
    if reference.is_symlink(): raise ValueError("参考 DTB 路径异常")
    current = {name: read_regular(root/name, optional=True) for name in TARGETS}
    current_modes = {name: (root/name).stat().st_mode & 0o777 for name,data in current.items() if data is not None}
    changed = []
    try:
        for name in reversed(TARGETS):
            changed.append(name)
            if previous[name] is not None:
                writer(root/name, previous[name]); os.chmod(root/name, modes[name])
            else: (root/name).unlink(missing_ok=True)
        reference.unlink(missing_ok=True)  # Disarm last, while the shared lock still excludes the guard.
    except (OSError, ValueError):
        for name in reversed(changed):
            if current[name] is None: (root/name).unlink(missing_ok=True)
            else: atomic(root/name, current[name], mode=current_modes[name])
        raise
    return {"restored": True, "reboot_required": True}


def rollback(backup, root="/", writer=atomic):
    with locked(root):
        return _rollback(backup, root, writer)


def rescan(root="/"):
    root = Path(root)
    model = running_property(root, "model")
    if model.rstrip(b"\0").decode() != MODEL: return {"scanned": 0}
    count = 0
    for path in sorted((root/"sys/class/scsi_host").glob("host*/scan")):
        path.write_text("- - -", encoding="ascii"); count += 1
    return {"scanned": count}


def state(root="/"):
    root = Path(root)
    running = running_property(root, "model").rstrip(b"\0").decode(errors="replace")
    result = {"model": running, "running_new": running == MODEL, "prepared": False,
              "reboot_required": False, "selected_new": False, "selection": "", "readable": True, "detail": ""}
    try:
        env = read_regular(root/"boot/fnEnv.txt")
        selected = selection(env); result["selection"] = selected
        target = boot_target(root, "boot/dtb/"+selected)
        current_hash = hashlib.sha256(read_regular(target)).hexdigest()
        result["selected_new"] = current_hash == SHA256
        result["prepared"] = selected == PIN and all(hashlib.sha256(read_regular(root/name)).hexdigest() == SHA256 for name in TARGETS[:2])
        result["reboot_required"] = current_hash in (SHA256, OLD_SHA256) and ((current_hash == SHA256) != result["running_new"])
    except (OSError, ValueError):
        result.update(readable=False, detail="引导配置尚未就绪或当前进程无权读取")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "status", "apply", "guard", "rollback", "rescan"])
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.action not in ("check", "status") and (not hasattr(os, "geteuid") or os.geteuid() != 0): parser.error("写操作需要 root")
    if args.action in ("check", "apply", "rollback") and not args.path: parser.error("缺少文件/备份路径")
    try:
        if args.action == "check": result = validate(Path(args.path).read_bytes())
        elif args.action == "status": result = state()
        elif args.action in ("apply", "rollback"):
            from s2deploy import Deployment
            result = Deployment().apply_dtb(args.path) if args.action == "apply" else Deployment().rollback_dtb(args.path)
        elif args.action == "rescan": result = rescan()
        else:
            if not Path("/var/lib/fnos-s2/dtb.ref").is_file(): result = {"changed": False, "detail": "未启用 DTB 守护"}
            else: result = guard()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error: parser.exit(1, "DTB 操作失败："+str(error)+"\n")
