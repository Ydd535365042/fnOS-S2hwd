#!/bin/bash
# =============================================================
# random-mac-apply.sh —— 按硬件 key 把固定 MAC 套用到当前网卡
#
# 由 udev 规则（add/move/change）调用，也供 random-mac.sh / fixup 直接调用。
# 用法： random-mac-apply.sh <硬件key>
#
# 关键点：
#   * 按硬件 key（PCI/USB/平台设备内核名）定位当前网卡，接口名变了也能找到。
#   * 当前 MAC 已是固定值则什么都不做，避免"设置触发 change 事件→又设置"的死循环。
#   * 优先原地设置（多数驱动支持，不掉线）；失败才 down/up 后设置。
# =============================================================
set -u

KEY="$1"
[ -n "$KEY" ] || exit 0
MAC_FILE="/etc/random-mac/$KEY"
[ -s "$MAC_FILE" ] || exit 0
mac=$(head -n1 "$MAC_FILE")

for iface in $(ls /sys/class/net 2>/dev/null); do
    p=$(readlink -f "/sys/class/net/$iface/device" 2>/dev/null) || continue
    [ "$(basename "$p")" = "$KEY" ] || continue

    cur=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
    [ "$cur" = "$mac" ] && exit 0

    if ip link set dev "$iface" address "$mac" 2>/dev/null; then
        logger -t random-mac "apply $iface($KEY): $cur -> $mac"
    else
        ip link set dev "$iface" down 2>/dev/null
        if ip link set dev "$iface" address "$mac" 2>/dev/null; then
            logger -t random-mac "apply $iface($KEY): $cur -> $mac (down/up)"
        else
            logger -t random-mac "apply $iface($KEY): FAILED"
        fi
        ip link set dev "$iface" up 2>/dev/null
    fi
    exit 0
done
exit 0
