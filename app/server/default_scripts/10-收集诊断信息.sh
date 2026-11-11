#!/bin/bash
# 只读：输出系统与服务状态，不读取 drivetemp 温度、不唤醒数据盘。
set -u
uname -a
uptime
python3 /usr/local/lib/fnos-s2/s2native.py
systemctl status fnos-s2-native.service fnos-s2-dtb-guard.service --no-pager || true
journalctl -u fnos-s2-native.service -n 60 --no-pager || true
