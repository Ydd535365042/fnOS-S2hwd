#!/bin/bash
# =============================================================
# s2-power-schedule.sh —— S2 定时开关机
#
# 配置 /etc/s2-power-schedule.conf：
#   ENABLED=0|1
#   SHUTDOWN_TIME=HH:MM   (空=不自动关机)
#   BOOT_TIME=HH:MM       (空=不自动开机)
#
# 关机：systemd timer 每日到点触发 poweroff
# 开机：把"下一次开机时刻"写入 RTC wakealarm，
#       设备软关机(standby)后由 RTC 在设定时刻唤醒（需主板 RTC 常供电，
#       即 NAS 一直插电；彻底断电则 RTC 复位，开机失效）。
#
# 两个入口，分工不同：
#   apply     开机时由 s2-power-schedule.service 调一次：写 timer + 设闹钟
#   arm-next  关机前由 s2-power-shutdown.service 调：只重设闹钟
#
# 为什么必须有 arm-next
# -------------------
# RTC 闹钟是【一次性】的：触发的那一刻内核就把它清掉了。
# 如果闹钟触发时设备恰好是开着的（某天 03:00 没关成机、或者用户一直在用），
# 闹钟就这么被消费掉，而 apply 只在开机时跑一次 —— 之后再也没有人重设，
# 定时开机会永久失效，直到有人手动开一次机。这种坏法是静默的，
# 用户只会觉得"定时开机时灵时不灵"。
# 所以在关机路径上再算一次"下一次开机时刻"，闭环才真正成立。
#
# 用法： s2-power-schedule.sh apply | arm-next | status
# =============================================================
set -u

CONF="/etc/s2-power-schedule.conf"
SHUTDOWN_TIMER="/etc/systemd/system/s2-power-shutdown.timer"
SHUTDOWN_SVC="s2-power-shutdown.service"

# RTC wakealarm 接口。原先在脚本加载时算一次存进变量 —— 如果 rtc 模块比本
# 服务晚加载，那一次就是空字符串，之后整个进程生命周期都不会再找，
# 定时开机静默失效。改成用到时现找，每次都是当下的真实情况。
find_rtc() {
  ls /sys/class/rtc/rtc*/wakealarm 2>/dev/null | head -1
}

read_conf() {
  ENABLED=0; SHUTDOWN_TIME=""; BOOT_TIME=""
  [ -f "$CONF" ] || return 0
  while IFS='=' read -r k v; do
    case "$k" in
      ENABLED)        ENABLED="$v" ;;
      SHUTDOWN_TIME)  SHUTDOWN_TIME="$v" ;;
      BOOT_TIME)      BOOT_TIME="$v" ;;
    esac
  done < "$CONF"
}

# $1=HH:MM -> 今天或明天该时刻的 epoch；若已过期则用明天
next_epoch() {
  local h m now today target
  IFS=':' read -r h m <<< "$1"
  now=$(date +%s)
  today=$(date -d "today ${h}:${m}" +%s 2>/dev/null)
  [ -z "$today" ] && return 1
  if [ "$today" -le "$now" ]; then
    target=$(date -d "tomorrow ${h}:${m}" +%s 2>/dev/null)
  else
    target=$today
  fi
  echo "$target"
}

arm_rtc() {
  local t="$1" rtc
  rtc=$(find_rtc)
  if [ -z "$rtc" ]; then
    echo "未找到 RTC wakealarm 接口，定时开机不可用"
    return 1
  fi
  # 先清零再写（部分 RTC 不允许重复写未来值）
  echo 0 > "$rtc" 2>/dev/null
  if echo "$t" > "$rtc" 2>/dev/null; then
    echo "已设置 RTC 定时开机: $(date -d "@$t" '+%F %T')"
    return 0
  fi
  # 回退 rtcwake
  if command -v rtcwake >/dev/null 2>&1; then
    if rtcwake -m no -t "$t" >/dev/null 2>&1; then
      echo "已通过 rtcwake 设置定时开机: $(date -d "@$t" '+%F %T')"
      return 0
    fi
  fi
  echo "RTC 定时开机设置失败（硬件可能不支持唤醒）"
  return 1
}

clear_rtc() {
  local rtc
  rtc=$(find_rtc)
  [ -n "$rtc" ] && echo 0 > "$rtc" 2>/dev/null
  return 0
}

apply() {
  read_conf
  if [ "$ENABLED" != "1" ]; then
    echo "定时开关机未启用，清理已设规则"
    systemctl disable --now "$SHUTDOWN_SVC"      >/dev/null 2>&1 || true
    systemctl disable --now s2-power-shutdown.timer >/dev/null 2>&1 || true
    clear_rtc
    return 0
  fi

  # ---- 定时关机 ----
  if [ -n "$SHUTDOWN_TIME" ]; then
    cat > "$SHUTDOWN_TIMER" <<EOF
[Unit]
Description=S2 定时关机定时器
[Timer]
OnCalendar=*-*-* ${SHUTDOWN_TIME}:00
Persistent=false
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload >/dev/null 2>&1
    systemctl enable --now s2-power-shutdown.timer >/dev/null 2>&1 \
      && echo "已启用定时关机: 每日 ${SHUTDOWN_TIME}" \
      || echo "定时关机启用失败"
  else
    systemctl disable --now s2-power-shutdown.timer >/dev/null 2>&1 || true
    echo "未设置关机时间，定时关机关闭"
  fi

  # ---- 定时开机 ----
  if [ -n "$BOOT_TIME" ]; then
    local t
    t=$(next_epoch "$BOOT_TIME") && arm_rtc "$t" \
      || echo "未设置有效的开机时间，定时开机关闭"
  else
    clear_rtc
    echo "未设置开机时间，定时开机关闭"
  fi
}

# 只重设 RTC 闹钟，不碰 timer —— 关机前调用，见文件头的说明。
#
# 本函数【永远返回 0】：s2-power-shutdown.service 里它和 poweroff 是两条
# ExecStart，Type=oneshot 下前一条非零退出会连带后一条不执行 ——
# 关机不能被"RTC 没设上"这种小事拦住。
arm_next() {
  read_conf
  if [ "$ENABLED" != "1" ] || [ -z "$BOOT_TIME" ]; then
    echo "定时开机未启用，跳过 RTC 闹钟"
    return 0
  fi
  local t
  if ! t=$(next_epoch "$BOOT_TIME"); then
    echo "开机时间格式非法（${BOOT_TIME}），跳过 RTC 闹钟"
    return 0
  fi
  arm_rtc "$t" || true
  return 0
}

case "${1:-apply}" in
  apply)    apply ;;
  arm-next) arm_next ;;
  status)   read_conf; echo "ENABLED=$ENABLED SHUTDOWN_TIME=$SHUTDOWN_TIME BOOT_TIME=$BOOT_TIME RTC=$(find_rtc)" ;;
  *) echo "usage: $0 [apply|arm-next|status]"; exit 1 ;;
esac
exit 0
