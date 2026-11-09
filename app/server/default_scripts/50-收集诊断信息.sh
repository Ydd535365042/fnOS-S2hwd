#!/bin/bash
# 出厂默认脚本 · 只读体检，不改任何配置（排障时先跑这个）
echo "===== 主机名 / 内核 ====="
hostname; uname -a
echo; echo "===== 温度与风扇 ====="
[ -r /var/lib/fan-control/status ] && cat /var/lib/fan-control/status || echo "(无状态文件)"
command -v fanctl >/dev/null && fanctl status | head -20
echo; echo "===== 硬盘 ====="
lsblk -d -o NAME,SIZE,MODEL 2>/dev/null
echo; echo "===== 硬件服务状态 ====="
for s in fan-control sata-led-manager sata-led-enable power-led-net power-key-daemon set-hostname selfheal boot-version-guard; do
    printf '%-20s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || echo '-')"
done
echo; echo "===== LED ====="
ls /sys/class/leds 2>/dev/null
echo; echo "===== 日志尾部 ====="
tail -n 15 /var/log/sata-led.log 2>/dev/null
