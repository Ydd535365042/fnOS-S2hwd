#!/usr/bin/env bash
# =============================================================
# set-hostname.sh —— 把设备名统一设为 fnOS-S2
#
# 为什么需要它
#   镜像出厂 hostname 是 "trim"（飞牛内部体系名），所以在路由器后台 /
#   局域网邻居里看到的就是 trim。这里把它改成 fnOS-S2，并同时改好：
#     1) /etc/hostname          —— 开机持久生效
#     2) /etc/hosts             —— 127.0.1.1 那条，否则本机解析会怪
#     3) /etc/avahi/avahi-daemon.conf —— mDNS 广播名（xxx.local）
#     4) 运行时的 kernel hostname —— 立即生效，DHCP 请求就会带上新名字
#   Samba 的 "server string = %h server" 用的是 %h（=hostname），
#   所以 Samba / wsdd2（Windows 网络邻居）也会跟着变，无需单独改。
#
# 幂等：重复执行结果一致，不会把 hosts / avahi 配置写乱。
#
# 想换名字：echo "MyNAS" > /etc/s2-hostname 再跑本脚本
# =============================================================
set -uo pipefail

NAME_FILE=/etc/s2-hostname
DEFAULT_NAME=fnOS-S2
NAME="$DEFAULT_NAME"

if [ -r "$NAME_FILE" ]; then
    _n=$(tr -d ' \t\r\n' < "$NAME_FILE" 2>/dev/null)
    [ -n "$_n" ] && NAME="$_n"
fi

log() { echo "[set-hostname] $*"; }

# ---------- 1) /etc/hostname ----------
if [ "$(cat /etc/hostname 2>/dev/null)" != "$NAME" ]; then
    echo "$NAME" > /etc/hostname 2>/dev/null && log "hostname -> $NAME"
else
    log "hostname already $NAME"
fi

# ---------- 2) /etc/hosts ----------
if [ -f /etc/hosts ]; then
    if grep -qE "^127\.0\.1\.1[[:space:]]" /etc/hosts; then
        sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$NAME/" /etc/hosts
        log "hosts 127.0.1.1 -> $NAME"
    else
        printf '127.0.1.1\t%s\n' "$NAME" >> /etc/hosts
        log "hosts 新增 127.0.1.1 $NAME"
    fi
    # 兜底：确保有 localhost 条目
    grep -qE "^127\.0\.0\.1[[:space:]]" /etc/hosts || printf '127.0.0.1\tlocalhost\n' >> /etc/hosts
fi

# ---------- 3) avahi（mDNS / xxx.local）----------
AVAHI=/etc/avahi/avahi-daemon.conf
if [ -f "$AVAHI" ]; then
    if grep -qE "^[[:space:]]*#?[[:space:]]*host-name[[:space:]]*=" "$AVAHI"; then
        sed -i "s|^[[:space:]]*#\?[[:space:]]*host-name[[:space:]]*=.*|host-name=$NAME|" "$AVAHI"
        log "avahi host-name -> $NAME"
    else
        sed -i "0,/^\[server\]/s|^\[server\]|[server]\nhost-name=$NAME|" "$AVAHI"
        log "avahi host-name 已插入 -> $NAME"
    fi
else
    log "avahi 配置不存在，跳过"
fi

# ---------- 4) 运行时立即生效 ----------
# 必须在网络起来之前跑（DHCP 请求会用当前 kernel hostname），
# 所以本服务是 Before=network-pre.target。
if command -v hostname >/dev/null 2>&1; then
    hostname "$NAME" 2>/dev/null || echo "$NAME" > /proc/sys/kernel/hostname 2>/dev/null || true
else
    echo "$NAME" > /proc/sys/kernel/hostname 2>/dev/null || true
fi
# chroot / 无 systemd 环境里 hostnamectl 不可用，忽略即可
command -v hostnamectl >/dev/null 2>&1 && hostnamectl set-hostname "$NAME" 2>/dev/null || true

log "当前生效主机名: $(cat /proc/sys/kernel/hostname 2>/dev/null)"
exit 0
