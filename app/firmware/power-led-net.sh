#!/bin/bash
# =============================================================
# power-led-net.sh —— 电源灯 = 网络链路状态指示灯
#
# 行为（默认只看物理链路，用户选定）
#   · 网线插上、链路通（网卡 carrier=1） → 绿灯常亮
#   · 没插网线 / 链路断开 / 无可用网口   → 红灯闪烁（0.5s 亮 / 0.5s 灭）
#
# 手动覆盖（优先级最高，S2硬件控制台 fpk 使用）
#   /etc/s2hwctl/led-override.conf 里 POWER_LED=off → 红绿全灭，
#   本脚本每个检测节拍都会重读该文件，因此固件升级 / selfheal 重装 /
#   服务重启都无法改变用户的手动选择。POWER_LED=auto 或文件不存在 = 自动模式。
#
# 与旧服务 power-led-solid.service 的关系
#   旧服务是「开机一次性把电源灯设为绿灯」，已被本服务取代。
#   install_all.sh / deploy.sh 会 disable 它；万一它还 enable，
#   本服务常驻，下一个节拍就会把灯纠正回网络状态。
#
# 可调参数：/etc/power-led-net.conf
# 手动查询：power-led-net.sh status      # 打印链路状态 + LED 路径
#            power-led-net.sh once        # 只检测并设置一次（不循环）
# =============================================================
set -u

CONF="${POWER_LED_NET_CONF:-/etc/power-led-net.conf}"
[ -f "$CONF" ] && . "$CONF"

LEDS_ROOT="${LEDS_ROOT:-/sys/class/leds}"
NET_ROOT="${NET_ROOT:-/sys/class/net}"

TICK="${TICK:-0.25}"                     # 主循环节拍（秒）
CHECK_EVERY="${CHECK_EVERY:-8}"          # 每几拍检测一次链路（8 × 0.25s = 2s）
BLINK_ON_TICKS="${BLINK_ON_TICKS:-2}"    # 红灯亮 2 拍 = 0.5s
BLINK_OFF_TICKS="${BLINK_OFF_TICKS:-2}"  # 红灯灭 2 拍 = 0.5s
LOG_FILE="${LOG_FILE:-/var/log/power-led-net.log}"
LOG_ENABLE="${LOG_ENABLE:-1}"
LED_OVERRIDE_CONF="${LED_OVERRIDE_CONF:-/etc/s2hwctl/led-override.conf}"

# 读 fpk 的手动覆盖（每个检测节拍重读一次，保证「全灭」优先级最高）
# 结果放全局 OVR（off|auto）。纯 bash 内建逐行读 KEY=VALUE，不 source、不开子 shell；
# 同名多行取最后一行（与 source 一致），引号与行尾注释能认。
OVR=auto
power_override() {
  local line v val=""
  OVR=auto
  [ -r "$LED_OVERRIDE_CONF" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in POWER_LED=*) ;; *) continue ;; esac
    v="${line#*=}"
    v="${v%%#*}"
    v="${v%"${v##*[![:space:]]}"}"
    v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
    val="$v"
  done < "$LED_OVERRIDE_CONF"
  [ "$val" = "off" ] && OVR=off
  return 0
}

# sleep 不 fork：对一个永不写入的 fd 做 read -t 超时（主循环 0.25 秒一拍，原来每秒 4 次 fork）。
# fd 开不起来、或 read 没等满就返回（不是超时码 >128）时，永久退回外部 sleep，绝不空转。
SNORE_FD=""
exec {SNORE_FD}<> <(:) || SNORE_FD=""
snore() {
  if [ -n "$SNORE_FD" ]; then
    local rc=0
    read -r -t "$1" -u "$SNORE_FD" _ 2>/dev/null || rc=$?
    [ "$rc" -gt 128 ] && return 0
    exec {SNORE_FD}<&-
    SNORE_FD=""
  fi
  sleep "$1"
}

log() {
  [ "$LOG_ENABLE" = "1" ] || return 0
  local ts
  printf -v ts '%(%F %T)T' -1
  echo "[$ts] $*" >> "$LOG_FILE" 2>/dev/null || true
  logger -t power-led-net "$*" 2>/dev/null || true
}

# ---------- LED 发现：优先 power:green / power:red，再退回任意同名色 ----------
led_dir() {
  local want="$1" d n
  for d in "$LEDS_ROOT"/*; do
    [ -d "$d" ] || continue
    n="${d##*/}"
    case "$n" in *power*"$want"*) printf '%s\n' "$d"; return 0 ;; esac
  done
  for d in "$LEDS_ROOT"/*"$want"*; do
    [ -d "$d" ] || continue
    printf '%s\n' "$d"; return 0
  done
  return 1
}

led_init() {  # 关掉 heartbeat/mmc0 之类自带 trigger，否则写 brightness 无效
  local d="$1"
  [ -n "$d" ] && [ -d "$d" ] || return 0
  echo none > "$d/trigger" 2>/dev/null || true
}

led_set() {
  local d="$1" v="$2"
  [ -n "$d" ] && [ -d "$d" ] || return 0
  echo "$v" > "$d/brightness" 2>/dev/null || true
}

# ---------- 链路检测：只看物理链路，不依赖 IP / 路由 / ping ----------
# carrier 文件只有在网卡处于 up 状态时才可读；接口 down 或不存在时读取失败 → 视为无链路。
# 找到的接口名放全局 LINK_IFACE（不再 printf 给 $(...) 捕获，省一次子 shell）。
LINK_IFACE=""
link_iface() {
  local d i c
  LINK_IFACE=""
  for d in "$NET_ROOT"/*; do
    [ -d "$d" ] || continue
    i="${d##*/}"
    case "$i" in
      lo|docker*|br-*|br[0-9]*|veth*|virbr*|vmnet*|tun*|tap*|dummy*|ifb*|sit*|gre*|wg*|zt*|tailscale*|ppp*) continue ;;
    esac
    # 2>/dev/null 放在 < 前面：接口 down 时 carrier 打不开，报错不进 journal
    read -r c 2>/dev/null < "$d/carrier" || continue
    if [ "$c" = "1" ]; then LINK_IFACE="$i"; return 0; fi
  done
  return 1
}

GREEN=""
RED=""
BLINK_LED=""

discover() {
  GREEN=$(led_dir green 2>/dev/null) || GREEN=""
  RED=$(led_dir red 2>/dev/null) || RED=""
  led_init "$GREEN"
  led_init "$RED"
  # 红灯不存在时（单色灯机器）用绿灯兼作指示：无网=绿灯闪，有网=绿灯常亮
  BLINK_LED="$RED"
  [ -z "$BLINK_LED" ] && BLINK_LED="$GREEN"
}

apply_state() {  # $1=up|down $2=接口名(可空)  只设置一次灯态，供 once 使用
  if [ "$1" = "up" ]; then
    led_set "$GREEN" 1
    led_set "$RED" 0
  else
    led_set "$GREEN" 0
    led_set "$BLINK_LED" 1
  fi
}

discover

# ---------- 非循环子命令 ----------
case "${1:-}" in
  status)
    if link_iface; then echo "link=up iface=$LINK_IFACE"; else echo "link=down"; fi
    power_override
    echo "override=$OVR"
    echo "green=${GREEN:-none}"
    echo "red=${RED:-none}"
    exit 0
    ;;
  once)
    if link_iface; then
      apply_state up "$LINK_IFACE"
      echo "link=up iface=$LINK_IFACE -> green solid"
    else
      apply_state down
      echo "link=down -> red blink (first tick)"
    fi
    exit 0
    ;;
esac

if [ -z "$GREEN" ] && [ -z "$RED" ]; then
  log "警告：未找到任何电源 LED，指示灯功能不可用（后续会周期性重试发现）"
else
  log "启动：green=${GREEN:-none} red=${RED:-none}（有链路→绿灯常亮，无链路→红灯闪烁）"
fi

last=""
tick=0
phase=0
blink_period=$(( BLINK_ON_TICKS + BLINK_OFF_TICKS ))
[ "$blink_period" -le 0 ] && blink_period=4

while true; do
  if [ $(( tick % CHECK_EVERY )) -eq 0 ]; then
    # LED 可能还没被 udev 创建，周期性重试发现
    if [ -z "$GREEN" ] && [ -z "$RED" ]; then
      discover
    else
      led_init "$GREEN"
      led_init "$RED"
    fi
    power_override
    if [ "$OVR" = "off" ]; then
      cur="override-off"
    elif link_iface; then
      cur="up:$LINK_IFACE"
    else
      cur="down"
    fi
    if [ "$cur" != "$last" ]; then
      case "$cur" in
        up:*)        log "链路已连接（${cur#up:}）→ 电源灯绿灯常亮" ;;
        override-off) log "手动覆盖（POWER_LED=off）→ 电源灯红绿全灭" ;;
        *)           log "无网络链路 → 电源灯红灯闪烁" ;;
      esac
      last="$cur"
    fi
  fi

  case "$last" in
    up:*)
      led_set "$GREEN" 1
      led_set "$RED" 0
      phase=0
      ;;
    override-off)
      # 手动全灭：优先级最高，红绿（含闪烁指示灯）全部压灭并持续保持
      led_set "$GREEN" 0
      led_set "$RED" 0
      led_set "$BLINK_LED" 0
      phase=0
      ;;
    *)
      led_set "$GREEN" 0
      if [ "$phase" -lt "$BLINK_ON_TICKS" ]; then
        led_set "$BLINK_LED" 1
      else
        led_set "$BLINK_LED" 0
      fi
      phase=$(( phase + 1 ))
      [ "$phase" -ge "$blink_period" ] && phase=0
      ;;
  esac

  tick=$(( tick + 1 ))
  snore "$TICK"
done
