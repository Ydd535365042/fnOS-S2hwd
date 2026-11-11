#!/bin/bash
# 停用本项目的美化服务，恢复安装美化前的服务启用状态；不修改引导。
set -eu
python3 /usr/local/lib/fnos-s2/s2deploy.py lights-off
