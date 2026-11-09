#!/bin/bash
# 出厂默认脚本 · 用 fpk 内嵌的 deploy.sh 重新部署全部硬件定制脚本（安全、不重启）
# 事实来源：app/server/default_scripts/ —— 安装回调从这里复制；误删补回走 POST /api/script/defaults
set -u
source "$(dirname "$0")/_common.sh"
for d in $(_s2_firmware_dirs); do
  if [ -f "$d/deploy.sh" ]; then
    echo "==> 部署: $d/deploy.sh"
    bash "$d/deploy.sh" "$d"
    echo "==> 完成，退出码 $?"
    exit 0
  fi
done
echo "!! 未找到 deploy.sh（fpk 内嵌目录或 /usr/local/share/install-1014）"
exit 1
