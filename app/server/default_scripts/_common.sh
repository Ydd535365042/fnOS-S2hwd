#!/bin/bash
# 出厂默认脚本 · 公共函数库（下划线开头，不会出现在脚本库列表里）
# 事实来源只有这一份：cmd/install_callback 安装时复制它，
# /api/script/defaults 恢复默认脚本时也复制它。改这里就是改两处。
#
# 返回所有存在的硬件脚本源目录（fpk 内嵌优先）
_s2_firmware_dirs() {
  local FPK="${TRIM_APPDEST:-/var/apps/fnOS-S2}"
  for d in "$FPK/firmware" "$FPK/target/firmware" \
           /usr/local/share/install-1014 /home/anna/install-1014; do
    [ -d "$d" ] && echo "$d"
  done
}
# 在候选目录里找第一个存在的文件 $1，输出绝对路径；找不到返回非零
_s2_find_file() {
  local f
  for d in $(_s2_firmware_dirs); do
    if [ -f "$d/$1" ]; then echo "$d/$1"; return 0; fi
  done
  return 1
}
