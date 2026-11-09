#!/bin/bash
# 出厂默认脚本 · 把定制 DTB 装回 /boot/dtb（升级内核后 SATA/LED 失效时用它）
set -u
DST=/boot/dtb/rockchip/rk3568-nanopi-r5s.dtb
source "$(dirname "$0")/_common.sh"
SRC="$(_s2_find_file rk3568-nanopi-r5s-new.dtb)"
if [ -z "$SRC" ]; then echo "!! 找不到定制 DTB 源文件"; exit 1; fi
echo "==> $SRC -> $DST"
cp -f "$SRC" "$DST" && echo "==> 已恢复，重启后生效"
md5sum "$SRC" "$DST"
