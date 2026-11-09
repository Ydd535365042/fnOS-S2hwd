#!/bin/bash
# 出厂默认脚本 · 调用自愈脚本体检（--no-reboot，只修复不打扰正在用的机器）
set -u
source "$(dirname "$0")/_common.sh"
SRC="$(_s2_find_file selfheal-check.sh)"
if [ -n "$SRC" ]; then
  bash "$SRC" --no-reboot
else
  echo "!! 未找到 selfheal-check.sh"
  exit 1
fi
