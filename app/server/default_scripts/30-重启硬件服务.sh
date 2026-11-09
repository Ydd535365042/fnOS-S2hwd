#!/bin/bash
# 出厂默认脚本 · 重启本机的硬件相关服务
for s in fan-control sata-led-manager sata-led-enable power-led-net power-key-daemon random-mac selfheal set-hostname boot-version-guard; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${s}.service"; then
        echo "==> restart $s"
        systemctl restart "$s" 2>&1 | head -3
        systemctl is-active "$s" 2>/dev/null | sed "s/^/   状态: /"
    fi
done
echo "==> 完成"
