#!/bin/bash
# 选装：开机闪灯及提示音、关机混色及提示音；不修改 DTB / fnEnv。
set -eu
python3 /usr/local/lib/fnos-s2/s2deploy.py lights
