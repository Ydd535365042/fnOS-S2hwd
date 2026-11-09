#!/usr/bin/env bash
set -euo pipefail

# Unified installer for RK3568 NAS-like setup:
# - Device tree
# - Boot beep + power-key beep
# - Power LED solid green
# - SATA LED policy (idle green, io blink, error red)
# - Fan control service
# - Author: witxiaohui
# - Date: 2026-08-22 优化更新版
# - 无人值守补丁（v3）：
#     1、两处 read 加超时 + 默认值，stdin 为 EOF 时不再被 set -e 中断
#     2、重启交给 boot-version-guard.sh 统一处理，本脚本不再调用 reboot
#     3、依赖文件缺失只跳过并告警，不让 set -e 把后面 daemon-reload / 启服务全打断
#     4、所有 systemctl 调用加 timeout：卡死的 daemon-reload / enable --now /
#        restart 会拖死整条链路，让 guard 根本走不到重启那一步
#     5、显式 exit 0，保证 guard 拿到干净的结束信号
#
# - 快重启补丁（v4）——本版重点：把 install 阶段的"等待"全部砍掉
#     A) 新增 --pre-reboot 参数（boot-version-guard 在"装完立即重启"时传入）：
#        此模式下【只做落盘 + enable】，跳过所有 start / restart / disable --now。
#        理由：马上就要重启，服务是否此刻起来毫无意义，重启后 systemd 会自动拉起；
#        而 enable --now 是阻塞调用，5 个服务逐个等启动是最主要的耗时来源。
#     B) 交互提示改为「先判断 stdin 是不是终端」（[[ -t 0 ]]）：
#        非交互下【根本不 read】，从源头消除固定空等，
#        也不用再依赖 stdin 恰好是 /dev/null 才会立刻 EOF 这种脆弱假设。
#     C) 所有 systemctl 超时从 120/180s 收紧到 20/60s：
#        正常调用 0.1~2s 就返回，旧值只在卡死时才有意义，
#        但卡死时 180s 会把 guard 的重启时机白白推迟几分钟。
#     D) 结尾 wait 一次，确保 tee 子进程把日志刷完再退出。

if [[ "${EUID}" -ne 0 ]]; then
  echo "❌ 请使用root权限运行: sudo bash $0"
  exit 1
fi

# ---- 参数解析（放最前，保证 --pre-reboot 一定生效）----
# 也可以用环境变量 PRE_REBOOT=1 传入；命令行参数优先，
# 这样即使经过 sudo 的 env_reset 也不会丢失。
PRE_REBOOT="${PRE_REBOOT:-0}"
for arg in "$@"; do
  case "$arg" in
    --pre-reboot)    PRE_REBOOT=1 ;;
    --no-pre-reboot) PRE_REBOOT=0 ;;
  esac
done
[[ "${PRE_REBOOT}" == "1" ]] && echo "⚡ PRE_REBOOT 模式：只落盘 + enable，跳过服务启动，装完立刻交回 guard 重启"

DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${DIR}/install_${TS}.log"

# 统一日志输出，同时打印到终端和日志文件
exec > >(tee -a "${LOG_FILE}") 2>&1

SRC_DTB="${DIR}/rk3568-nanopi-r5s-new.dtb"
DST_DTB="/boot/dtb/rockchip/rk3568-nanopi-r5s.dtb"
MISSING_FILES=()

# systemctl 包装：超时 + 永不因失败中断整脚本
# 超时从 120s 收紧到 20s：正常 0.1~2s 返回，20s 足够覆盖开机高峰期的抖动
SCTL_TMO="${SCTL_TMO:-20}"
sctl() {
  timeout "${SCTL_TMO}" systemctl "$@" 2>&1 \
    && return 0 \
    || { echo "❌ systemctl $* 失败或超时（已跳过，不影响后续）"; return 0; }
}

# 启动服务：PRE_REBOOT 下只 enable，绝不阻塞等启动
sctl_start() {
  local svc="$1"
  if [[ "${PRE_REBOOT}" == "1" ]]; then
    if timeout "${SCTL_TMO}" systemctl enable "${svc}" 2>&1; then
      echo "✅ ${svc} 已 enable（PRE_REBOOT：跳过 start，重启后由 systemd 拉起）"
    else
      echo "❌ ${svc} enable 失败或超时，请重启后排查"
    fi
    return 0
  fi
  # 非 PRE_REBOOT（调试 / REBOOT=0）才真正启动，超时 180→60s
  if timeout 60 systemctl enable --now "${svc}"; then
    echo "✅ ${svc} 启动成功"
  else
    echo "❌ ${svc} 启动失败或超时，可稍后排查"
  fi
  return 0
}

# 前置校验所有依赖文件是否存在
REQUIRED_FILES=(
  "beep-short.sh" "beep-boot.service"
  "power-led-solid.sh" "power-led-solid.service"
  "power-led-net.sh" "power-led-net.service" "power-led-net.conf"
  "power-key.sh" "triggerhappy-power-key.conf"
  "power-key-daemon.py" "power-key-daemon.service" "power-key-logind.conf"
  "random-mac.sh" "random-mac.service" "random-mac-apply.sh" "random-mac-fixup.service"
  "selfheal-check.sh" "selfheal.service" "selfheal-hourly.service" "selfheal-hourly.timer"
  "set-hostname.sh" "set-hostname.service"
  "sata-led-enable.sh" "sata-led-enable.service" "sata-led-manager.sh" "sata-led-manager.service" "sata-led.conf"
  "fan-control.sh" "fan-control.service" "fan-control.conf" "fanctl"
  "fan-api.py" "fan-api.service" "fan-api.conf" "drivetemp.conf"
  "s2-power-schedule.sh" "s2-power-schedule.service" "s2-power-shutdown.service" "s2-power-shutdown.timer" "s2-power-schedule.conf"
)

for f in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "${DIR}/${f}" ]]; then
    MISSING_FILES+=("${DIR}/${f}")
  fi
done

if [[ ${#MISSING_FILES[@]} -gt 0 ]]; then
  echo "⚠️  检测到缺失以下依赖文件，对应组件将被跳过："
  printf "  - %s\n" "${MISSING_FILES[@]}"
  # 只有真人在终端前才问；非交互一律默认继续，不再有任何 read 空等
  if [[ -t 0 ]]; then
    read -r -t 10 -p "是否继续安装? [Y/N]: " miss_confirm || miss_confirm="Y"
    [[ ! "${miss_confirm}" =~ ^[Yy]$ ]] && echo "安装已中止" && exit 1
  else
    echo "ℹ️  非交互模式：默认继续安装"
  fi
fi

# 单个文件安装：源不存在就跳过，绝不让 set -e 把整脚本打断
inst() {
  local src="$1" dst="$2" mode="$3"
  if [[ -f "${src}" ]]; then
    install -m "${mode}" "${src}" "${dst}"
  else
    echo "⚠️  跳过（源文件缺失）: ${src}"
  fi
}

# 用户在控制台里改的配置只在不存在时放出厂默认（自愈会调本脚本，不能把用户设置改回去）。
# 见 deploy.sh 里 inst_conf 的说明。
inst_conf() {
  if [[ -f "$2" ]]; then
    echo "保留现有配置: $2"
    return 0
  fi
  inst "$@"
}

mkdir -p /etc/triggerhappy/triggers.d
echo "📦 开始安装组件..."

inst "${DIR}/beep-short.sh" /usr/local/sbin/beep-short.sh 755
inst "${DIR}/beep-boot.service" /etc/systemd/system/beep-boot.service 644

inst "${DIR}/power-led-solid.sh" /usr/local/sbin/power-led-solid.sh 755
inst "${DIR}/power-led-solid.service" /etc/systemd/system/power-led-solid.service 644

# 电源灯 = 网络链路指示（有链路→绿灯常亮，无链路→红灯闪烁）
inst "${DIR}/power-led-net.sh" /usr/local/sbin/power-led-net.sh 755
inst "${DIR}/power-led-net.service" /etc/systemd/system/power-led-net.service 644
inst_conf "${DIR}/power-led-net.conf" /etc/power-led-net.conf 644

inst "${DIR}/power-key.sh" /usr/bin/power-key.sh 755
inst "${DIR}/triggerhappy-power-key.conf" /etc/triggerhappy/triggers.d/power-key.conf 644

# 电源键长按守护（镜像内没有 triggerhappy，triggers.d 无人执行；
# 短按即关机是 logind 的默认行为，必须把电源键交给本守护进程）
mkdir -p /etc/systemd/logind.conf.d
inst "${DIR}/power-key-daemon.py" /usr/local/sbin/power-key-daemon.py 755
inst "${DIR}/power-key-daemon.service" /etc/systemd/system/power-key-daemon.service 644
inst "${DIR}/power-key-logind.conf" /etc/systemd/logind.conf.d/10-power-key.conf 644

# 每台设备烧录后自动随机 MAC（首次启动生成、之后永久固定）
inst "${DIR}/random-mac.sh" /usr/local/sbin/random-mac.sh 755
inst "${DIR}/random-mac.service" /etc/systemd/system/random-mac.service 644
inst "${DIR}/random-mac-apply.sh" /usr/local/sbin/random-mac-apply.sh 755
inst "${DIR}/random-mac-fixup.service" /etc/systemd/system/random-mac-fixup.service 644

# 自愈自检：飞牛 apt 升级内核会把官方 DTB 装回 /boot，本组件开机校验
# DTB 指纹 + 关键文件，被还原就自动重装（不依赖 /etc/issue 的版本行）
inst "${DIR}/selfheal-check.sh" /usr/local/sbin/selfheal-check.sh 755
inst "${DIR}/selfheal.service" /etc/systemd/system/selfheal.service 644
inst "${DIR}/selfheal-hourly.service" /etc/systemd/system/selfheal-hourly.service 644
inst "${DIR}/selfheal-hourly.timer" /etc/systemd/system/selfheal-hourly.timer 644

# 把安装源备份一份到 /usr/local/share（apt 升级不会碰这里），
# 万一 /home/anna/install-1014 被清，selfheal 能原样恢复
if [[ -d "${DIR}" ]]; then
  mkdir -p /usr/local/share/install-1014
  cp -a "${DIR}/." /usr/local/share/install-1014/ 2>/dev/null
  echo "✅ 安装源已备份至: /usr/local/share/install-1014"
fi

# 设备名：镜像出厂 hostname 是 "trim"，在路由器后台 / 局域网里显示的就是它。
# 这里统一改成 fnOS-S2（/etc/hostname + /etc/hosts + avahi mDNS 名），
# 在网络起来之前生效，DHCP 请求才会带上新名字。
inst "${DIR}/set-hostname.sh" /usr/local/sbin/set-hostname.sh 755
inst "${DIR}/set-hostname.service" /etc/systemd/system/set-hostname.service 644

inst "${DIR}/sata-led-enable.sh" /usr/local/sbin/sata-led-enable.sh 755
inst "${DIR}/sata-led-enable.service" /etc/systemd/system/sata-led-enable.service 644
inst "${DIR}/sata-led-manager.sh" /usr/local/sbin/sata-led-manager.sh 755
inst "${DIR}/sata-led-manager.service" /etc/systemd/system/sata-led-manager.service 644
inst_conf "${DIR}/sata-led.conf" /etc/sata-led.conf 644

# 风扇控制 v4：硬盘温度参与调速 + auto/manual 双模式（页面手动调节预留）
#  - /var/lib/fan-control/{mode,level} 是运行态控制文件，页面后端直接写即可
#  - fanctl 是命令行入口；fan-api.py 是可选 HTTP 接口（默认不启用）
inst "${DIR}/fan-control.sh" /usr/local/bin/fan-control.sh 755
inst "${DIR}/fan-control.service" /etc/systemd/system/fan-control.service 644
inst_conf "${DIR}/fan-control.conf" /etc/fan-control.conf 644
inst "${DIR}/fanctl" /usr/local/bin/fanctl 755
mkdir -p /var/lib/fan-control
echo "✅ 风扇运行态目录就绪: /var/lib/fan-control"

# 风扇 HTTP 接口：只安装、不启用，留给后期页面接入时再开
#   启用命令： systemctl enable --now fan-api   （默认仅监听 127.0.0.1:8899）
inst "${DIR}/fan-api.py" /usr/local/sbin/fan-api.py 755
inst "${DIR}/fan-api.service" /etc/systemd/system/fan-api.service 644
inst_conf "${DIR}/fan-api.conf" /etc/fan-api.conf 644
echo "ℹ️  fan-api 已安装但【未启用】，页面接入时执行: systemctl enable --now fan-api"

# 开机加载 drivetemp：硬盘温度走内核 hwmon，避免 smartctl 反复唤醒待机盘
inst "${DIR}/drivetemp.conf" /etc/modules-load.d/drivetemp.conf 644

# 定时开关机：每日定时关机(systemd timer + poweroff) + 定时开机(RTC wakealarm)
inst "${DIR}/s2-power-schedule.sh" /usr/local/sbin/s2-power-schedule.sh 755
inst "${DIR}/s2-power-schedule.service" /etc/systemd/system/s2-power-schedule.service 644
inst "${DIR}/s2-power-shutdown.service" /etc/systemd/system/s2-power-shutdown.service 644
inst "${DIR}/s2-power-shutdown.timer" /etc/systemd/system/s2-power-shutdown.timer 644
inst_conf "${DIR}/s2-power-schedule.conf" /etc/s2-power-schedule.conf 644
echo "ℹ️  定时开关机已安装（默认关闭，由控制台「定时开关机」卡片配置后生效）"

if [[ -f "${SRC_DTB}" ]]; then
  if [[ -f "${DST_DTB}" ]]; then
    BACKUP_DTB="${DST_DTB}.bak_${TS}"
    cp -a "${DST_DTB}" "${BACKUP_DTB}"
    echo "✅ 原DTB已备份至: ${BACKUP_DTB}"
  fi
  install -m 644 "${SRC_DTB}" "${DST_DTB}"
  echo "✅ DTB安装完成: ${DST_DTB}"
else
  echo "⚠️  ${SRC_DTB} 未找到，跳过DTB安装"
fi

echo "🔄 重载systemd配置..."
sctl daemon-reload

# 禁用旧的冲突LED服务
# PRE_REBOOT 下不要 --now：--now 会阻塞等它完全停掉，而马上就要重启了
if [[ "${PRE_REBOOT}" == "1" ]]; then
  timeout "${SCTL_TMO}" systemctl disable HDled_monit_v0.service 2>/dev/null || true
else
  timeout 30 systemctl disable --now HDled_monit_v0.service 2>/dev/null || true
fi

# 禁掉飞牛自带的 PWM 风扇服务：它会读 /boot/board.json 去抢同一个 PWM 通道，
# 和我们的 fan-control 打架。当前它本来就没 enable，这里只是兜底。
if [[ "${PRE_REBOOT}" == "1" ]]; then
  timeout "${SCTL_TMO}" systemctl disable pwm-fancontrol.service 2>/dev/null || true
else
  timeout 30 systemctl disable --now pwm-fancontrol.service 2>/dev/null || true
fi

echo "🚀 处理所有新服务..."
# 电源灯交给 power-led-net 常驻服务接管，旧的「开机强制绿灯」服务停用
if [[ "${PRE_REBOOT}" == "1" ]]; then
  timeout "${SCTL_TMO}" systemctl disable power-led-solid.service 2>/dev/null || true
else
  timeout 30 systemctl disable --now power-led-solid.service 2>/dev/null || true
fi

SERVICES=(beep-boot.service power-led-net.service sata-led-enable.service sata-led-manager.service fan-control.service power-key-daemon.service random-mac.service random-mac-fixup.service set-hostname.service selfheal.service s2-power-schedule.service)
for svc in "${SERVICES[@]}"; do
  sctl_start "${svc}"
done

# 每小时自检（只修复、不重启；enable 后由 timers.target 拉起，无需 --now）
timeout "${SCTL_TMO}" systemctl enable selfheal-hourly.timer 2>&1 || true

# triggerhappy：PRE_REBOOT 下不 restart，新配置重启后自然加载
if timeout "${SCTL_TMO}" systemctl list-unit-files triggerhappy.service &>/dev/null; then
  if [[ "${PRE_REBOOT}" == "1" ]]; then
    echo "ℹ️  PRE_REBOOT：跳过 triggerhappy 重启，新按键配置将在重启后生效"
  else
    sctl restart triggerhappy.service
  fi
fi

echo
echo "🎉 所有组件安装完成，安装日志已保存到: ${LOG_FILE}"
# 注意：这里【不要】 wait。开头 `exec > >(tee ...)` 让 bash 的 stdout 指向 tee，
# 此时 wait 会等 tee 退出、而 tee 正等 bash 关闭 stdout → 互锁死等，脚本永不返回。
# bash 退出时会自动关闭 fd，tee 收到 EOF 后自行 flush 退出，日志不会丢。

# PRE_REBOOT：不做任何交互、不自己重启，立即把控制权交回 guard
if [[ "${PRE_REBOOT}" == "1" ]]; then
  echo "⚡ PRE_REBOOT 完成，交回 boot-version-guard.sh 执行重启"
  exit 0
fi

echo "ℹ️  修改DTB后强烈建议重启设备使其生效"
if [[ -t 0 ]]; then
  read -r -t 5 -p "现在重启设备? [Y/N]: " reboot_answer || reboot_answer="N"
  if [[ "${reboot_answer}" =~ ^[Yy]$ ]]; then
    echo "🔌 由本脚本触发重启..."
    reboot
  else
    echo "ℹ️  跳过重启"
  fi
else
  echo "ℹ️  非交互模式：跳过本脚本自带的重启，由 boot-version-guard.sh 负责重启"
fi

exit 0
