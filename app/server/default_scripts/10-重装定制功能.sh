#!/bin/bash
# 出厂默认脚本 · 完整重装（install_all.sh，含 enable --now，部分项需重启生效）
set -u
source "$(dirname "$0")/_common.sh"
SRC="$(_s2_find_file install_all.sh)"
if [ -n "$SRC" ]; then
  echo "==> 使用安装源: $SRC"
  D="$(dirname "$SRC")"
  bash "$SRC" --pre-reboot
  echo "==> 完成，退出码 $?"
  exit 0
fi
echo "!! 未找到 install_all.sh"
exit 1
