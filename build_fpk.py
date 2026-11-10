"""把 app + fpk 源码目录打成 fnOS 应用包，格式照抄原 1.6.2 包：
  外层 .fpk = gzip 压缩的 GNU tar，成员 root/root，cmd/config/wizard 目录 + 图标 + LICENSE + manifest + app.tgz
  app.tgz   = gzip 压缩的 GNU tar，成员 root/root，文件 0o705（原包就是这么给的，脚本靠它有执行位）
  manifest 的 checksum = app.tgz 的 MD5（原包里核对过：27a567a5… 正好是原 app.tgz 的 MD5）

不改源目录：manifest 在内存里改版本号 / changelog / checksum 后直接写进包。
排除 __pycache__ / *.pyc / 测试沙箱残留。打完再解开自检一遍。

用法：python -I build_fpk.py <app> <fpk> <输出.fpk> <版本号> <changelog文本文件>"""
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP, FPK, OUT, VER, CHANGELOG = sys.argv[1:6]  # 例：app fpk out.fpk 1.0.0 changelog.txt
MTIME = int(time.time())
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
SKIP_EXT = (".pyc", ".pyo", ".tmp", ".bak", ".orig")


def info(name, size, mode, is_dir=False):
    ti = tarfile.TarInfo(name)
    ti.size = 0 if is_dir else size
    ti.mode = mode
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    ti.mtime = MTIME
    return ti


def lf_only(path, data):
    # 脚本、单元文件、配置不能带 CRLF：bash 会把 \r 当成命令的一部分
    if path.endswith((".png", ".dtb", ".PNG")) or b"\0" in data[:4096]:
        return data
    if b"\r\n" in data:
        print("  修正 CRLF →", path)
        data = data.replace(b"\r\n", b"\n")
    return data


def tar_gz(entries):
    """entries: [(arcname, bytes|None(目录), mode)] → gzip 字节"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as t:
        for name, data, mode in entries:
            if data is None:
                t.addfile(info(name, 0, mode, True))
            else:
                t.addfile(info(name, len(data), mode), io.BytesIO(data))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=MTIME) as g:
        g.write(raw.getvalue())
    return buf.getvalue()


# 出厂脚本直接使用 Git 源文件名，不依赖旧版 app.tgz。
ORIG_NAMES = {}
for filename in os.listdir(os.path.join(APP, "server", "default_scripts")):
    match = re.match(r"^(\d\d)-.+\.sh$", filename)
    if match:
        if match.group(1) in ORIG_NAMES:
            raise SystemExit("出厂脚本编号重复: " + match.group(1))
        ORIG_NAMES[match.group(1)] = filename


# ---------- app.tgz ----------
app_entries = []
for root, dirs, files in os.walk(APP):
    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
    rel = os.path.relpath(root, APP).replace("\\", "/")
    if rel != ".":
        app_entries.append((rel, None, 0o705))
    for fn in sorted(files):
        if fn.endswith(SKIP_EXT):
            continue
        p = os.path.join(root, fn)
        name = fn
        arc = (name if rel == "." else rel + "/" + name)
        with open(p, "rb") as f:
            data = lf_only(arc, f.read())
        app_entries.append((arc, data, 0o705))
app_tgz = tar_gz(app_entries)
md5 = hashlib.md5(app_tgz).hexdigest()
print("app.tgz: %d 个文件，%d 字节，MD5 %s" % (sum(1 for e in app_entries if e[1] is not None), len(app_tgz), md5))

# ---------- manifest ----------
with open(os.path.join(FPK, "manifest"), "r", encoding="utf-8") as f:
    man = f.read()
with open(CHANGELOG, "r", encoding="utf-8") as f:
    cl = " ".join(f.read().split())


def setkey(text, key, val):
    pat = re.compile(r"^(%s\s*=\s*).*$" % re.escape(key), re.M)
    assert pat.search(text), "manifest 里没有 " + key
    return pat.sub(lambda m: m.group(1) + val, text, count=1)


man = setkey(man, "version", VER)
man = setkey(man, "changelog", cl)
man = setkey(man, "checksum", md5)
man_b = man.encode("utf-8")

# ---------- 外层 .fpk（成员顺序照原包） ----------
outer = []
for d in ("cmd", "config", "wizard"):
    outer.append((d, None, 0o705))
for fn in ("ICON.PNG", "ICON_256.PNG", "LICENSE"):
    with open(os.path.join(FPK, fn), "rb") as f:
        outer.append((fn, f.read(), 0o705))
outer.append(("app.tgz", app_tgz, 0o644))
for d in ("cmd", "config"):
    for fn in sorted(os.listdir(os.path.join(FPK, d))):
        with open(os.path.join(FPK, d, fn), "rb") as f:
            outer.append((d + "/" + fn, lf_only(d + "/" + fn, f.read()), 0o705))
outer.append(("manifest", man_b, 0o705))
for fn in sorted(os.listdir(os.path.join(FPK, "wizard"))):
    with open(os.path.join(FPK, "wizard", fn), "rb") as f:
        outer.append(("wizard/" + fn, lf_only("wizard/" + fn, f.read()), 0o705))
fpk = tar_gz(outer)
os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
with open(OUT, "wb") as f:
    f.write(fpk)
print("写出 %s：%d 字节" % (OUT, len(fpk)))

# ---------- 自检：重新解开，核对 checksum、版本、关键文件、权限 ----------
errs = []
t = tarfile.open(OUT, "r:gz")
names = t.getnames()
for need in ("manifest", "app.tgz", "cmd/main", "cmd/install_callback", "cmd/upgrade_callback", "wizard/install", "config/privilege"):
    if need not in names:
        errs.append("外层缺 " + need)
m2 = t.extractfile("manifest").read().decode("utf-8")
a2 = t.extractfile("app.tgz").read()
if re.search(r"^checksum\s*=\s*(\S+)", m2, re.M).group(1) != hashlib.md5(a2).hexdigest():
    errs.append("checksum 不等于 app.tgz 的 MD5")
if re.search(r"^version\s*=\s*(\S+)", m2, re.M).group(1) != VER:
    errs.append("版本号没写进去")
if any(x.uname != "root" or x.gname != "root" for x in t.getmembers()):
    errs.append("外层有非 root 成员")
ta = tarfile.open(fileobj=io.BytesIO(a2), mode="r:gz")
an = set(ta.getnames())
for need in ("server/server.py", "server/hwctl.py", "server/hwinfo.py", "server/alerts.py", "server/public/app.js",
             "server/s2security.py", "server/public/login.js", "agent/install-hw-agent.sh",
             "server/s2store.py", "server/s2ops.py", "server/s2hardware.py", "server/job_runner.py",
             "server/public/manage.html", "server/public/manage.js", "server/public/manage.css", "firmware/s2_schedule.py",
             "server/public/index.html", "server/public/style.css", "agent/s2hw-agent.py", "firmware/deploy.sh",
             "firmware/fan-control.sh", "server/default_scripts/00-部署全部硬件配置.sh", "server/default_scripts/_common.sh"):
    if need not in an:
        errs.append("app.tgz 缺 " + need)
ds = sorted(n.rsplit("/", 1)[1] for n in an if n.startswith("server/default_scripts/") and n.endswith(".sh"))
if sorted(ORIG_NAMES.values()) != [d for d in ds if re.match(r"^\d\d-", d)]:
    errs.append("出厂脚本名和源码对不上: %s" % ds)
print("出厂脚本:", ds)
bad = [x.name for x in ta.getmembers() if "__pycache__" in x.name or x.name.endswith(".pyc")]
if bad:
    errs.append("混进了缓存文件: %s" % bad[:3])
noexec = [x.name for x in ta.getmembers() if x.isfile() and x.name.endswith((".sh", ".py")) and not x.mode & 0o100]
if noexec:
    errs.append("脚本没有执行位: %s" % noexec[:3])
crlf = []
for x in ta.getmembers():
    if x.isfile() and x.name.endswith((".sh", ".py", ".service", ".conf", ".timer")) or x.name.startswith("bin/"):
        if x.isfile() and b"\r\n" in ta.extractfile(x).read():
            crlf.append(x.name)
if crlf:
    errs.append("仍有 CRLF: %s" % crlf[:3])
for x in t.getmembers():
    if x.name.startswith("cmd/") and x.isfile() and b"\r\n" in t.extractfile(x).read():
        errs.append("cmd 脚本有 CRLF: " + x.name)
print("自检：外层 %d 个成员，app.tgz %d 个成员" % (len(names), len(an)))
if errs:
    print("自检失败：")
    for e in errs:
        print("  ✗", e)
    sys.exit(1)
print("自检通过：checksum = MD5(app.tgz) = %s，version = %s" % (md5, VER))
