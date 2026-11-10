#!/bin/bash
# 只读：检查标准硬件节点，不刷 DTB、不启动服务、不写 GPIO。
set -eu
python3 /usr/local/lib/fnos-s2/s2native.py
python3 /usr/local/lib/fnos-s2/s2dtb.py status
printf '\n当前设备树：\n'
tr '\0' '\n' < /proc/device-tree/model
printf '\n当前引导选择：\n'
grep '^fdtfile=' /boot/fnEnv.txt || true
printf '\n控制服务：\n'
systemctl is-active fnos-s2-native.service s2-power-schedule.service || true
