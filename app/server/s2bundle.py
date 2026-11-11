"""Run pinned, complete vendor packages through the existing module interface."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def write_state(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError("状态文件不能是符号链接")
    fd, tmp = tempfile.mkstemp(prefix=".bundle-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream: stream.write(value)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def execute(directory, action, root="", run=subprocess.run):
    directory = Path(directory)
    meta = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    ident = meta["id"]
    if ident not in ("s2-selfheal", "s2-lights") or action not in ("apply", "remove", "status", "check"):
        raise ValueError("不支持的内置模块操作")
    package = directory / "package.run"
    if hashlib.sha256(package.read_bytes()).hexdigest() != meta["sha256"]:
        raise ValueError("内置安装包校验失败，请重新安装应用")
    root = Path(root) if root else Path("/")
    if root != Path("/") and not root.is_absolute():
        raise ValueError("隔离目录必须是绝对路径")
    live = root == Path("/")
    if action in ("apply", "remove") and live and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        raise ValueError("内置硬件模块需要以 root 运行，请检查应用权限")
    environment = os.environ.copy()
    environment.pop("S2_FORCE_LIVE", None)
    if not live: environment["S2_ROOT"] = str(root)
    else: environment.pop("S2_ROOT", None)
    if action in ("apply", "remove"):
        checked = run(["bash", str(package), "--check"], env=environment)
        if checked.returncode: raise ValueError("原包完整性或依赖检查失败，尚未变更系统")
    beep_config = None
    if ident == "s2-lights" and action == "apply":
        beep = root / "etc/s2hwctl/beep.conf"
        beep_config = json.loads(beep.read_text(encoding="utf-8")) if beep.exists() else {}
        if not isinstance(beep_config, dict): raise ValueError("蜂鸣配置格式无效，请先修复配置")
    flags = {"status": ["--status"], "check": ["--check"],
             "apply": ["--install"] if ident == "s2-selfheal" else [],
             "remove": ["--uninstall", "--yes"] if ident == "s2-selfheal" else ["--revert"]}[action]
    if action == "apply" and ident == "s2-selfheal":
        model = root / "proc/device-tree/model"
        model_text = model.read_text(errors="replace") if model.exists() else ""
        old = root / "boot/dtb/rockchip/rk3568-nanopi-r5s.dtb"
        known = old.is_file() and hashlib.sha256(old.read_bytes()).hexdigest() in (
            "38943953170941897b8dcbf41f3018fcd5fb13bb6c13e564348811ba60da5d5c",
            "6dc95aac914dfe5618f09027d31190ae0b84a7efdcc797fa0fa39ecfbbeeb509")
        if "S2" not in model_text and "witxiaohui" not in model_text.lower() and not known:
            raise ValueError("未识别为适配的 S2 设备，未修改引导文件")
    changed_boot = False
    if live and action in ("apply", "remove"):
        mount = run(["findmnt", "-n", "-o", "OPTIONS", "/boot"], capture_output=True, text=True)
        changed_boot = mount.returncode == 0 and "ro" in mount.stdout.strip().split(",")
    print(f'{meta["title"]} {meta["version"]} · {action}', flush=True)
    try:
        result = run(["bash", str(package), *flags], env=environment)
    finally:
        if changed_boot:
            restored = run(["mount", "-o", "remount,ro", "/boot"], capture_output=True, text=True)
            if restored.returncode:
                print("警告：/boot 未恢复只读，请检查挂载状态", file=sys.stderr)
    if result.returncode:
        raise ValueError(f'原包执行失败（退出码 {result.returncode}），请查看上方完整日志')
    marker = directory / "enabled"
    if action == "apply":
        for installed in meta.get("installed_files", []):
            path = root / installed["path"].lstrip("/")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != installed["sha256"]:
                raise ValueError("安装后的文件校验失败：" + installed["path"])
        if live:
            units = ["fnos-s2-dtb-guard.path", "fnos-s2-dtb-guard.service"] if ident == "s2-selfheal" else ["fnos-s2-led-boot.service", "fnos-s2-led-shutdown.service"]
            run(["systemctl", "disable", "--now", *units], capture_output=True, text=True)
            for unit in units:
                if run(["systemctl", "is-active", "--quiet", unit], capture_output=True, text=True).returncode == 0:
                    raise ValueError("旧服务仍在运行，请检查：" + unit)
        write_state(marker, meta["version"] + "\n")
    elif action == "remove":
        marker.unlink(missing_ok=True)
    if ident == "s2-lights" and action in ("apply", "remove"):
        native_marker = root / "etc/s2hwctl/led-beautify.enabled"
        if action == "apply":
            native_marker.parent.mkdir(parents=True, exist_ok=True)
            write_state(native_marker, "vendor\n")
            beep = native_marker.with_name("beep.conf")
            config = beep_config
            config["BEEP_PROMPT"] = config.get("boot_beep", True)
            config["BEEP_BOOT"] = config["BEEP_PROMPT"]
            write_state(beep, json.dumps(config))
        else:
            native_marker.unlink(missing_ok=True)
    if action == "apply":
        print("安装自检完成。启动阶段效果和新设备树需在下次正常重启后生效。", flush=True)
    return meta


if __name__ == "__main__":
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else "status"
        lock = None
        if action in ("apply", "remove") and os.name == "posix":
            import fcntl
            lock_path = Path(os.environ.get("S2_ROOT") or "/") / "run/fnos-s2-bundles.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            if lock_path.is_symlink(): raise ValueError("模块锁路径无效")
            lock = lock_path.open("a+")
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError("另一个内置模块正在操作，请完成后重试")
        execute(Path(__file__).parent, action, os.environ.get("S2_ROOT", ""))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
