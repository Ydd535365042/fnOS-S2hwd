#!/bin/bash
# =============================================================
# deploy.sh —— 由 s2hwctl fpk「安装即部署」模式调用
#
# 作用：把 app/firmware 下的全部 RK3568 硬件定制脚本部署到系统并启用服务，
#       使任意 fnOS 固件（不止定制镜像）装上本 fpk 后即可获得完整的
#       电源灯 / 硬盘灯 / 风扇 / 蜂鸣 / 电源键 / 随机 MAC / 主机名 / 自愈 行为。
#
# 设计约束（重要）：
#   1) 不使用 set -e，单步失败只记录、不中断，脚本始终以 exit 0 结束，
#      避免 fpk 安装被判为失败。
#   2) 不主动重启设备。DTB / 主机名 / 随机 MAC 等需要重启才完全生效的部分
#      仅启用并给出提示；fan / led / power-key / beep / selfheal 立即生效。
#   3) DTB 仅在板子匹配（目标文件存在）时才安装，防止刷错板把 DTB 写坏。
#   4) 部署前先把本目录备份到 /usr/local/share/install-1014，供 selfheal 恢复。
#
# 用法： bash deploy.sh [FIRMWARE_DIR]
# =============================================================
set -u

SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
LOG="/var/log/s2hwctl-deploy.log"

ts() { date '+%F %T'; }
log() { echo "$(ts) - $*" | tee -a "$LOG" 2>/dev/null; }

if [ "$(id -u)" -ne 0 ]; then
  log "需要 root 权限（fpk 以 root 安装，正常情况下不应走到这里）"
  exit 0
fi

# 单文件安装：源缺失则跳过，绝不中断
inst() {
  local src="$1" dst="$2" mode="$3"
  if [ -f "$src" ]; then
    if install -m "$mode" "$src" "$dst" 2>/dev/null; then
      log "已安装: $dst"
    else
      log "安装失败（已跳过）: $dst"
    fi
  else
    log "跳过（源缺失）: $src"
  fi
}

# 用户在控制台里改的配置（风扇曲线、硬盘灯、电源灯、定时开关机）：只在不存在时放出厂默认。
# 以前和程序文件一样每次都覆盖 —— 自愈每小时一查，缺一个文件就重跑安装，
# 用户设好的风扇曲线、定时开关机被静默改回出厂值（定时开关机会直接变成「关闭」）。
# 新版本新增的键不必合并：各守护脚本都先给全部默认值、再 source 配置覆盖。
inst_conf() {
  local src="$1" dst="$2" mode="$3"
  if [ -f "$dst" ]; then
    log "保留现有配置: $dst"
    return 0
  fi
  inst "$src" "$dst" "$mode"
}

mkdir -p /etc/triggerhappy/triggers.d \
         /etc/systemd/logind.conf.d \
         /var/lib/fan-control \
         /usr/local/share/install-1014

log "开始部署 S2 硬件定制脚本（来源: $SRC）"

# ---- 开机蜂鸣 ----
inst "$SRC/beep-short.sh"          /usr/local/sbin/beep-short.sh 755
inst "$SRC/beep-boot.service"      /etc/systemd/system/beep-boot.service 644

# ---- 电源灯 = 网络链路指示（有链路绿灯常亮，无链路红灯闪烁）----
inst "$SRC/power-led-net.sh"        /usr/local/sbin/power-led-net.sh 755
inst "$SRC/power-led-net.service"   /etc/systemd/system/power-led-net.service 644
inst_conf "$SRC/power-led-net.conf"      /etc/power-led-net.conf 644
# 旧的「开机强制绿灯」服务停用，电源灯交给 power-led-net 常驻接管
inst "$SRC/power-led-solid.sh"     /usr/local/sbin/power-led-solid.sh 755
inst "$SRC/power-led-solid.service" /etc/systemd/system/power-led-solid.service 644

# ---- 电源键长按守护 ----
inst "$SRC/power-key.sh"           /usr/bin/power-key.sh 755
inst "$SRC/triggerhappy-power-key.conf" /etc/triggerhappy/triggers.d/power-key.conf 644
inst "$SRC/power-key-daemon.py"    /usr/local/sbin/power-key-daemon.py 755
inst "$SRC/power-key-daemon.service" /etc/systemd/system/power-key-daemon.service 644
inst "$SRC/power-key-logind.conf"  /etc/systemd/logind.conf.d/10-power-key.conf 644

# ---- 随机 MAC（每台唯一、刷机后永久固定）----
inst "$SRC/random-mac.sh"          /usr/local/sbin/random-mac.sh 755
inst "$SRC/random-mac.service"     /etc/systemd/system/random-mac.service 644
inst "$SRC/random-mac-apply.sh"    /usr/local/sbin/random-mac-apply.sh 755
inst "$SRC/random-mac-fixup.service" /etc/systemd/system/random-mac-fixup.service 644

# ---- 自愈（DTB / 关键文件被升级还原时自动重装）----
inst "$SRC/selfheal-check.sh"      /usr/local/sbin/selfheal-check.sh 755
inst "$SRC/selfheal.service"       /etc/systemd/system/selfheal.service 644
inst "$SRC/selfheal-hourly.service" /etc/systemd/system/selfheal-hourly.service 644
inst "$SRC/selfheal-hourly.timer"  /etc/systemd/system/selfheal-hourly.timer 644

# ---- 备份安装源（供 selfheal 原样恢复）----
if cp -a "$SRC/." /usr/local/share/install-1014/ 2>/dev/null; then
  log "已备份安装源至: /usr/local/share/install-1014"
else
  log "备份失败（不影响部署）"
fi

# ---- 设备名 fnOS-S2 ----
inst "$SRC/set-hostname.sh"        /usr/local/sbin/set-hostname.sh 755
inst "$SRC/set-hostname.service"   /etc/systemd/system/set-hostname.service 644

# ---- 硬盘灯 ----
inst "$SRC/sata-led-enable.sh"     /usr/local/sbin/sata-led-enable.sh 755
inst "$SRC/sata-led-enable.service" /etc/systemd/system/sata-led-enable.service 644
inst "$SRC/sata-led-manager.sh"    /usr/local/sbin/sata-led-manager.sh 755
inst "$SRC/sata-led-manager.service" /etc/systemd/system/sata-led-manager.service 644
inst_conf "$SRC/sata-led.conf"          /etc/sata-led.conf 644

# ---- 风扇 ----
inst "$SRC/fan-control.sh"         /usr/local/bin/fan-control.sh 755
inst "$SRC/fan-control.service"    /etc/systemd/system/fan-control.service 644
inst_conf "$SRC/fan-control.conf"       /etc/fan-control.conf 644
inst "$SRC/fanctl"                 /usr/local/bin/fanctl 755
# 风扇 HTTP 接口：安装不启用，留给页面后期接入
inst "$SRC/fan-api.py"             /usr/local/sbin/fan-api.py 755
inst "$SRC/fan-api.service"        /etc/systemd/system/fan-api.service 644
inst_conf "$SRC/fan-api.conf"           /etc/fan-api.conf 644
# 硬盘温度走内核 hwmon
inst "$SRC/drivetemp.conf"         /etc/modules-load.d/drivetemp.conf 644

# ---- 开机版本守卫 ----
inst "$SRC/boot-version-guard.sh"  /usr/local/bin/boot-version-guard.sh 755
inst "$SRC/boot-version-guard.service" /etc/systemd/system/boot-version-guard.service 644

# ---- 定时开关机 ----
inst "$SRC/s2-power-schedule.sh"      /usr/local/sbin/s2-power-schedule.sh 755
inst "$SRC/s2-power-schedule.service" /etc/systemd/system/s2-power-schedule.service 644
inst "$SRC/s2-power-shutdown.service" /etc/systemd/system/s2-power-shutdown.service 644
inst "$SRC/s2-power-shutdown.timer"   /etc/systemd/system/s2-power-shutdown.timer 644
inst_conf "$SRC/s2-power-schedule.conf"    /etc/s2-power-schedule.conf 644

# ---- 定制 DTB：仅当板子匹配才安装 ----
SRC_DTB="$SRC/rk3568-nanopi-r5s-new.dtb"
DST_DTB="/boot/dtb/rockchip/rk3568-nanopi-r5s.dtb"
if [ -f "$SRC_DTB" ] && [ -f "$DST_DTB" ]; then
  cp -a "$DST_DTB" "${DST_DTB}.bak_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
  if install -m 644 "$SRC_DTB" "$DST_DTB" 2>/dev/null; then
    log "已安装定制 DTB: $DST_DTB（重启后生效）"
  else
    log "DTB 安装失败（已跳过）"
  fi
elif [ -f "$SRC_DTB" ]; then
  log "目标 DTB 不存在($DST_DTB)，跳过 DTB 安装（可能非 R5S 板）"
else
  log "未找到定制 DTB 源，跳过"
fi

# ---- 重载 systemd ----
systemctl daemon-reload >/dev/null 2>&1 || true
log "已重载 systemd"

# ---- 禁用冲突服务 ----
systemctl disable HDled_monit_v0.service >/dev/null 2>&1 || true
systemctl disable pwm-fancontrol.service >/dev/null 2>&1 || true
systemctl disable power-led-solid.service >/dev/null 2>&1 || true

# ---- 启用并启动常驻服务（立即生效）----
for svc in beep-boot.service power-led-net.service sata-led-enable.service \
          sata-led-manager.service fan-control.service power-key-daemon.service \
          random-mac.service random-mac-fixup.service set-hostname.service selfheal.service \
          boot-version-guard.service s2-power-schedule.service; do
  if [ -f "/etc/systemd/system/$svc" ] || systemctl list-unit-files "$svc" >/dev/null 2>&1; then
    systemctl enable "$svc" >/dev/null 2>&1
    systemctl restart "$svc" >/dev/null 2>&1
    log "已启用/重启: $svc"
  fi
done

# 定时关机定时器默认不启用（仅当用户在控制台配置时间后才由脚本 enable）
if [ -f "/etc/systemd/system/s2-power-shutdown.timer" ]; then
  systemctl disable s2-power-shutdown.timer >/dev/null 2>&1 || true
  log "定时关机定时器已就位（未启用，待用户在控制台配置）"
fi

# 每小时自检（只修复、不重启）
systemctl enable selfheal-hourly.timer >/dev/null 2>&1 && \
systemctl restart selfheal-hourly.timer >/dev/null 2>&1 && \
log "已启用: selfheal-hourly.timer"

log "S2 硬件定制脚本部署完成（部分项需重启生效；可在控制台「脚本修复」随时重部署）"
exit 0
