#!/bin/bash
# 危险操作：写 /boot/dtb 与 /boot/fnEnv.txt；仅适用于已核验的 S2 新旧 DTB。
# 升级前自动备份；不会自动重启。请在脚本审查中核对 SHA256 与风险后再运行。
set -eu
python3 /usr/local/lib/fnos-s2/s2dtb.py check /usr/local/lib/fnos-s2/rk3568-witxiaohui-s2.dtb
python3 /usr/local/lib/fnos-s2/s2deploy.py dtb
printf '\nDTB 已准备好，请手动安排重启；原生硬件控制在新 DTB 启动后接管。\n'
