#!/bin/bash
# 危险操作：还原上次显式升级前的 /boot/fnEnv.txt 和 DTB。
# 保留当前内核控制直至重启，恢复旧服务下次开机的启用状态；不会自动重启。
set -eu
python3 /usr/local/lib/fnos-s2/s2deploy.py rollback-dtb
