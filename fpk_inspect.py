"""对比原 .fpk / app.tgz 与当前源码：打包格式、权限、新增 / 删除的文件。
用法：python -I fpk_inspect.py <原.fpk> <原app.tgz> <_extracted_app 目录>"""
import collections
import os
import sys
import tarfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
fpk, app, src = sys.argv[1], sys.argv[2], sys.argv[3]
with open(fpk, "rb") as f:
    head = f.read(2)
print("outer:", "gzip" if head == b"\x1f\x8b" else "plain tar")
t = tarfile.open(fpk)
print("outer format:", t.format, "members", len(t.getmembers()))
for x in t.getmembers():
    print("   %-24s %s %s/%s" % (x.name, oct(x.mode), x.uname, x.gname))
t2 = tarfile.open(app, "r:gz")
am = t2.getmembers()
print("app.tgz members", len(am), "format", t2.format)
for k, v in collections.Counter((oct(x.mode), x.type, x.uname, x.gname) for x in am).most_common():
    print("  ", k, v)
orig = {x.name.rstrip("/") for x in am if x.isfile()}
cur = set()
for root, dirs, files in os.walk(src):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for fn in files:
        cur.add(os.path.relpath(os.path.join(root, fn), src).replace("\\", "/"))
print("ADDED:", sorted(cur - orig))
print("REMOVED:", sorted(orig - cur))
print("exec in original:", sorted(x.name for x in am if x.isfile() and x.mode & 0o111))
pyc = []
for root, dirs, files in os.walk(src):
    if "__pycache__" in root:
        pyc.append(root)
print("__pycache__ dirs in source:", pyc)
