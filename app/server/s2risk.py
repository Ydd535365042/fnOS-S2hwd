"""Bounded static script review hints, never a claim that arbitrary shell is safe."""
import hashlib
from pathlib import Path
import re


def inspect(content, name, factory):
    factory = Path(factory)
    path = factory/Path(name).name
    known = path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(content).digest()
    if known and name in ("00-硬件自检.sh", "10-收集诊断信息.sh"):
        return []
    if known and name == "20-升级S2设备树.sh":
        return ["写入两份 S2 DTB 与 /boot/fnEnv.txt，并保留升级前备份", "停止旧自愈并启用引导守护；完成后需手动安排重启"]
    if known and name == "30-安装电源灯美化.sh":
        return ["安装并启用开机、关机指示灯与提示音服务，不修改引导文件"]
    text = content.decode("utf-8", "replace")
    rules = [
        (r"/boot(?:/|\b)|fnEnv|fdtfile|s2deploy\.py[^\n]*\bdtb\b|\b(?:flash_erase|nandwrite|mkfs|wipefs)\b", "包含引导或磁盘相关操作，请核对写入目标"),
        (r"\b(?:systemctl|chattr|modprobe|insmod|rmmod|mount|umount)\b|/etc/|s2deploy\.py", "包含系统服务、配置或内核操作"),
        (r"\b(?:curl|wget)\b|https?://", "包含网络地址或下载命令，请核对下载内容"),
        (r"\b(?:eval|base64|openssl)\b|__S2_PAYLOAD_BELOW__", "包含动态执行或编码载荷，文本预览不能展示全部行为"),
        (r"\brm\s+[^\n]*(?:-[^\s]*r|--recursive)", "包含递归删除命令，请核对目录"),
    ]
    findings = [message for pattern, message in rules if re.search(pattern, text, re.I)]
    if b"\0" in content or len(content) > 65536: findings.append("文件含二进制或超出完整预览范围")
    return findings
