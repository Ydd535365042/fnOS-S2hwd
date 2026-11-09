#!/bin/bash
# =============================================================
# random-mac.sh —— 每台设备唯一、刷机/首次启动后永久固定的随机 MAC
#
# 设计目标（解决"用着用着 MAC 就变"）：
#   * 首次启动为每块物理网卡生成一个「本地管理单播」随机 MAC，
#     持久化到 /etc/random-mac/<稳定硬件key>。
#   * 之后【永远】读取同一份记录，绝不重新生成 —— 即使接口名因内核 /
#     驱动版本在 eth0 ↔ enp2s0... 之间变化，也按"硬件标识"定位，
#     不会当成新设备重新随机。
#   * 通过 udev 规则（add / move / change）在网卡每次出现、改名、或被
#     改 MAC 时强制套用固定值；fnOS 自身网络配置也无法改掉。
#   * 另由 random-mac-fixup.service 在网络就绪后再兜底套用一次。
#   * 重新烧录新镜像后 /etc/random-mac 不存在 → 又是全新 MAC。
#
# 重建随机（换一台设备 / 想换号时）：
#   rm -rf /etc/random-mac /etc/udev/rules.d/00-random-mac.rules
#   systemctl restart random-mac.service
# =============================================================
set -u

STATE_DIR=/etc/random-mac
RULES=/etc/udev/rules.d/00-random-mac.rules
LOG_TAG=random-mac

mkdir -p "$STATE_DIR"

gen_mac() {
    local r b1
    r=$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')
    [ "${#r}" -eq 12 ] || return 1
    # bit0(I/G)=0 单播，bit1(U/L)=1 本地管理：第二位十六进制为 2/6/A/E
    b1=$(( (0x${r:0:2} | 0x02) & 0xfe ))
    printf '%02x:%s:%s:%s:%s:%s' \
        "$b1" "${r:2:2}" "${r:4:2}" "${r:6:2}" "${r:8:2}" "${r:10:2}"
}

is_physical() {
    local iface=$1
    case "$iface" in
        lo|docker*|br-*|veth*|virbr*|wl*|ww*|tun*|tap*|bond*|dummy*|sit*|gre*) return 1 ;;
    esac
    # 虚拟接口在 /sys/class/net/<x>/device 上没有指向物理设备的链接
    [ -e "/sys/class/net/$iface/device" ] || return 1
    return 0
}

# 稳定硬件 key：网卡父设备（PCI/USB/平台）的内核名，不随接口名变化
hw_key() {
    local iface=$1 p
    p=$(readlink -f "/sys/class/net/$iface/device" 2>/dev/null) || return 1
    basename "$p"
}

# 生成 udev 规则（每台设备只含已固定的 MAC，udev 负责每次强制套用）
echo "# 由 random-mac.sh 生成：每台设备固定的随机 MAC，请勿手改" > "$RULES"

count=0
for iface in $(ls /sys/class/net 2>/dev/null); do
    is_physical "$iface" || continue
    key=$(hw_key "$iface") || { logger -t "$LOG_TAG" "$iface: 取硬件key失败"; continue; }
    f="$STATE_DIR/$key"
    if [ -s "$f" ]; then
        mac=$(head -n1 "$f")
        logger -t "$LOG_TAG" "$iface($key): 复用已固定 MAC $mac"
    else
        mac=$(gen_mac) || { logger -t "$LOG_TAG" "$iface($key): 生成失败"; continue; }
        echo "$mac" > "$f"
        logger -t "$LOG_TAG" "$iface($key): 生成新 MAC $mac"
    fi
    # 网卡每次出现 / 改名 / 被改 MAC 都强制套用固定值
    # （apply 脚本在 current==mac 时原地无操作，不会因设置触发 change 而进入死循环）
    echo "ACTION==\"add|move|change\", SUBSYSTEM==\"net\", KERNELS==\"$key\", RUN+=\"/usr/local/sbin/random-mac-apply.sh $key\"" >> "$RULES"
    # 立即套用（交给统一 apply 脚本，逻辑一致）
    /usr/local/sbin/random-mac-apply.sh "$key" 2>/dev/null || true
    count=$((count + 1))
done

udevadm control --reload 2>/dev/null || true
logger -t "$LOG_TAG" "done, $count interface(s)"
exit 0
