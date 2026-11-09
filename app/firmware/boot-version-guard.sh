#!/usr/bin/env bash
# =============================================================
# boot-version-guard.sh  （v4：非阻塞调度 + 确认 + 退出 + 逐级硬兜底 + 全程计时）
#
# 每次开机采集「fnOS 系统版本 + 内核版本」；
# 若与上次开机记录不一致 → 更新记录 → 执行安装脚本 → 重启设备
#
# 用法：
#   bash boot-version-guard.sh                # 开机检查模式（服务调用）
#   bash boot-version-guard.sh --init         # 仅采集并记录当前版本作为基线
#   bash boot-version-guard.sh --test-reboot  # 只测重启链路（会真的重启！）
#
# ---------------- v3 相比 v2 改了什么（为什么 v2 仍可能不重启）----------------
# v2 在 guard 进程里【阻塞】执行 `systemctl reboot -f`，同时 service 里配了
# KillMode=none。这两件事叠在一起会出问题：
#   1) systemd 开始关机 → 要停止 boot-version-guard.service；
#   2) KillMode=none → systemd 不杀它，只能【等它自己退出】；
#   3) 但 guard 正阻塞在 systemctl 里，而 systemctl 又在等关机推进；
#      → 停止作业迟迟不完，关机流程卡住，设备就"没重启"。
# 另外 v2 的 `systemctl reboot -f` 用的是阻塞模式，本身也容易被
# shutdown 流程反噬（v1 的后台进程被 cgroup 杀，v2 的前台进程被停服卡住）。
#
# v3 的思路：把"发起"和"完成"彻底解耦
#   第 1 级：systemctl --no-block reboot -f  → 【立即返回】，只负责排期
#   验   证：轮询确认 systemd 真的把关机排上了（scheduled 文件 /
#            is-system-running=stopping / 本服务进入 deactivating）
#   确   认：立刻 exit 0，主动让路，systemd 才能顺利走完关机 → 重启
#   未确认：说明 logind 这条路不通，才升级到硬手段
#   第 2 级：systemctl reboot -f -f（直接 reboot(2)，绕开 PID1/logind）
#   第 3 级：SysRq  s(同步) → u(只读重挂) → b(重启)
# 这样 guard 在"优雅路径"下【已经退出了】，不存在自杀/被杀/死锁。
#
# ---------------- v4 相比 v3 改了什么（把"等待"砍到最短）----------------
# v3 的重排期思路是对的，但真正拖慢设备的是【重启之前的固定空等】：
#   1) BOOT_DELAY=15s 开机空等；service 又 After=network-online.target，
#      而本脚本只需要读 /etc/issue + 跑本地脚本，压根不需要网络 →
#      网络慢时这里能白等 30~90s（真正的最大元凶，见 setup 脚本里 unit 的修改）
#   2) install_all.sh 里 5 个服务逐个 `enable --now` 阻塞等启动
#      → v4 通过 --pre-reboot 参数让它只 enable 不 start
#   3) REBOOT_DELAY=5s 装完后的空等
#   4) 排期确认轮询粒度 1s、上限 30s，而 scheduled 文件通常 <1s 就出现
#      → v4 改成 0.25s 粒度、上限 12s，正常 ~0.5s 内确认并退出
#   5) INSTALL_TIMEOUT=3600s：卡死时要等一小时才轮到重启 → 收紧到 900s
# 另外 v4 给每个阶段打了耗时日志（elapsed），哪一段慢一眼就能看出来。
#
# 可调环境变量：
#   ISSUE_FILE       /etc/issue
#   STATE_DIR        记录存放目录
#   INSTALL_SCRIPT   要执行的安装脚本
#   BOOT_DELAY       开机后延迟秒数，默认 3（v3 是 15；网络已不再等待，无需那么久）
#   REBOOT           1=装完重启（默认）；0=不重启（调试）
#   REBOOT_DELAY     装完后到重启之间的等待秒数，默认 1（v3 是 5）
#   SCHED_WAIT       第 1 级排期确认的等待秒数，默认 12（v3 是 30）
#   FEED_Y           0=不给任何输入（默认）；1=用 yes Y 应答脚本提问
#                    （v4 起 install_all.sh 非交互不再 read，一般不需要开）
#   INSTALL_TIMEOUT  安装脚本硬超时秒数，默认 900（v3 是 3600）
#   SYNC_TIMEOUT     sync 的最长等待秒数，默认 10
#   PRE_REBOOT       1=告知 install_all.sh 跳过服务启动（REBOOT=1 时自动为 1）
# =============================================================
set -uo pipefail

ISSUE_FILE="${ISSUE_FILE:-/etc/issue}"
STATE_DIR="${STATE_DIR:-/var/lib/boot-version-guard}"
STATE_FILE="$STATE_DIR/last_version"
LOG_FILE="${LOG_FILE:-/var/log/boot-version-guard.log}"
LOCK_FILE="${LOCK_FILE:-/var/lock/boot-version-guard.lock}"
INSTALL_SCRIPT="${INSTALL_SCRIPT:-/usr/local/share/install-1014/install_all.sh}"
BOOT_DELAY="${BOOT_DELAY:-3}"
REBOOT="${REBOOT:-1}"
REBOOT_DELAY="${REBOOT_DELAY:-1}"
SCHED_WAIT="${SCHED_WAIT:-12}"
FEED_Y="${FEED_Y:-0}"
INSTALL_TIMEOUT="${INSTALL_TIMEOUT:-900}"
SYNC_TIMEOUT="${SYNC_TIMEOUT:-10}"

mkdir -p "$STATE_DIR" "$(dirname "$LOCK_FILE")" 2>/dev/null
touch "$LOG_FILE" 2>/dev/null

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE" 2>/dev/null; }

# 开机以来的秒数（用来判断"设备到底等了多久"）
uptime_s() { awk '{printf "%d", $1}' /proc/uptime 2>/dev/null || echo '?'; }
# 阶段耗时
T_START="$(date +%s)"
elapsed() { echo "$(( $(date +%s) - T_START ))"; }

# 支持小数的 sleep；busybox 等不支持小数的环境下退回 sleep 1
nap() { sleep "$1" 2>/dev/null || sleep 1 2>/dev/null || true; }

sync_disk() { timeout "$SYNC_TIMEOUT" sync 2>/dev/null || sync 2>/dev/null || true; }

# -------------------------------------------------------------
# 确认 systemd 真的把关机排上了（最多等 $1 秒）
# 三个判据任一命中即可：
#   a) /run/systemd/shutdown/scheduled 出现（最快，放最前面）
#   b) systemctl is-system-running 返回 stopping
#   c) 本服务进入 deactivating（说明 systemd 已经在停我们）
# 每个 systemctl 调用都套 timeout：关机过程中 D-Bus / PID1 可能变慢，
# 不能让"确认"这一步自己卡住，反而拖长重启。
# -------------------------------------------------------------
shutdown_scheduled() {
    [ -f /run/systemd/shutdown/scheduled ] && return 0
    case "$(timeout 2 systemctl is-system-running 2>/dev/null)" in
        stopping*) return 0 ;;
    esac
    [ "$(timeout 2 systemctl is-active boot-version-guard 2>/dev/null)" = "deactivating" ] && return 0
    return 1
}

wait_shutdown_scheduled() {
    local max="${1:-12}" i=0 step="0.25"
    local ticks=$(( max * 4 ))     # step=0.25s → 每秒 4 次
    while [ "$i" -lt "$ticks" ]; do
        shutdown_scheduled && return 0
        nap "$step"
        i=$((i + 1))
    done
    return 1
}

# -------------------------------------------------------------
# 重启：非阻塞调度 → 确认 → 主动退出；不通才升级到硬手段
# 优雅路径会直接 exit，不会返回；返回即代表该级失败
# -------------------------------------------------------------
reboot_now() {
    local t0
    log "========== 重启链路开始（PID $$，开机至今 $(uptime_s)s）=========="
    t0="$(date +%s)"
    sync_disk
    log "[0] sync 完成（$(($(date +%s) - t0))s）"

    # ---- 第 1 级：交给 systemd 正常关机（优雅：会停服务、umount、写日志）----
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        log "[1] systemctl --no-block reboot -f（只排期，不等完成）"
        t0="$(date +%s)"
        timeout 20 systemctl --no-block reboot -f 2>>"$LOG_FILE"
        log "[1] systemctl 返回码 $?（耗时 $(($(date +%s) - t0))s）"

        t0="$(date +%s)"
        if wait_shutdown_scheduled "$SCHED_WAIT"; then
            log "[1] ✅ 已确认关机排期（等待 $(($(date +%s) - t0))s）→ 本进程立即退出，让 systemd 完成重启"
            log "========== 重启链路结束（优雅路径，交给 systemd，开机至今 $(uptime_s)s）=========="
            exit 0
        fi
        log "[1] ❌ ${SCHED_WAIT}s 内未观察到关机排期，升级到硬手段"
    else
        log "[1] 跳过：非 systemd 环境"
    fi

    # ---- 第 2 级：直接 reboot(2) 系统调用，完全绕开 PID1 / logind / D-Bus ----
    log "[2] systemctl reboot -f -f（直接 reboot 系统调用，不卸载文件系统）"
    sync_disk
    timeout 20 systemctl reboot -f -f 2>>"$LOG_FILE"
    log "[2] 返回码 $?（成功则应已立刻重启，能返回即失败）"
    nap 6

    # ---- 第 3 级：Magic SysRq（s 同步 → u 只读重挂 → b 重启）----
    log "[3] SysRq 兜底"
    if [ -e /proc/sysrq-trigger ]; then
        if echo 1 >/proc/sys/kernel/sysrq 2>/dev/null; then
            log "[3] kernel.sysrq 已置 1"
        else
            log "[3] !! 无法写 /proc/sys/kernel/sysrq（内核可能未开 CONFIG_MAGIC_SYSRQ）"
        fi
        for k in s u b; do
            if echo "$k" >/proc/sysrq-trigger 2>/dev/null; then
                log "[3] sysrq '$k' 已下发"
            else
                log "[3] !! sysrq '$k' 下发失败"
            fi
            nap 2
        done
    else
        log "[3] !! /proc/sysrq-trigger 不存在，sysrq 兜底不可用"
    fi

    nap 12
    log "========== 重启链路全部走完，设备仍存活，已无更多手段 =========="
}

# ---- 参数 ----
INIT_MODE=0
TEST_REBOOT=0
for arg in "$@"; do
    case "$arg" in
        --init)        INIT_MODE=1 ;;
        --test-reboot) TEST_REBOOT=1 ;;
    esac
done

if [ "$TEST_REBOOT" -eq 1 ]; then
    log "========== --test-reboot 手动测试重启链路 =========="
    echo "3 秒后开始重启链路（会真的重启设备），Ctrl-C 可中止……"
    sleep 3
    reboot_now
    log "!! --test-reboot：reboot_now 返回，设备未能重启"
    exit 1
fi

# ---- 单实例锁 ----
# 子进程一律用 9>&- 关掉这个 fd，否则会长期持有 flock，
# 导致下次运行被误判为「已有实例」。
if [ "$INIT_MODE" -eq 0 ]; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "已有实例在运行，本次退出"
        exit 0
    fi
fi

if [ "$INIT_MODE" -eq 1 ]; then
    log "========== --init 记录基线 =========="
else
    log "========== 开机版本检查启动 (PID $$，开机至今 $(uptime_s)s) =========="
    [ "${BOOT_DELAY:-0}" -gt 0 ] 2>/dev/null && sleep "$BOOT_DELAY"
    log "BOOT_DELAY(${BOOT_DELAY}s) 结束，开机至今 $(uptime_s)s"
fi

# ---- 采集当前版本 ----
CUR_OS="$(grep -m1 -i '^OS version' "$ISSUE_FILE" 2>/dev/null \
          | sed -E 's/^[^:]*:[[:space:]]*//; s/[[:space:]]+$//')"
CUR_KERNEL="$(uname -r)"
CUR="${CUR_OS}|${CUR_KERNEL}"
log "当前版本: OS=${CUR_OS}  KERNEL=${CUR_KERNEL}"

if [ -z "$CUR_OS" ]; then
    log "未能读取系统版本（$ISSUE_FILE 无 'OS version' 行），跳过本次"
    if [ "$INIT_MODE" -eq 1 ]; then
        echo "[error] 未能读取系统版本（$ISSUE_FILE 无 'OS version' 行），未写入基线"
        exit 1
    fi
    exit 0
fi

# ---- --init：写入基线 ----
if [ "$INIT_MODE" -eq 1 ]; then
    OLD=""
    [ -s "$STATE_FILE" ] && OLD="$(head -n1 "$STATE_FILE" 2>/dev/null)"
    printf '%s\n' "$CUR" >"$STATE_FILE"
    echo "系统版本: ${CUR_OS}"
    echo "内核版本: ${CUR_KERNEL}"
    if [ -n "$OLD" ] && [ "$OLD" != "$CUR" ]; then
        echo "基线已更新: ${OLD} -> ${CUR}"; log "基线已更新: ${OLD} -> ${CUR}"
    elif [ -n "$OLD" ]; then
        echo "基线未变化: ${CUR}"; log "基线未变化: ${CUR}"
    else
        echo "已建立基线: ${CUR}"; log "已建立基线: ${CUR}"
    fi
    echo "基线文件: ${STATE_FILE}"
    log "========== --init 结束 =========="
    exit 0
fi

# ---- 首次运行：只写基线 ----
if [ ! -s "$STATE_FILE" ]; then
    printf '%s\n' "$CUR" >"$STATE_FILE"
    log "首次运行，已写入基线: ${CUR}（本次不执行安装）"
    log "========== 结束（耗时 $(elapsed)s）=========="
    exit 0
fi

LAST="$(head -n1 "$STATE_FILE" 2>/dev/null)"
log "上次版本: ${LAST}"

if [ "$CUR" = "$LAST" ]; then
    log "版本一致，无需操作"
    log "========== 结束（耗时 $(elapsed)s）=========="
    exit 0
fi

# ---- 版本发生变化：先落基线，避免重启后重复触发 ----
log ">>> 版本发生变化，更新记录"
printf '%s\n' "$CUR" >"$STATE_FILE"

if [ ! -f "$INSTALL_SCRIPT" ]; then
    log "安装脚本不存在: $INSTALL_SCRIPT —— 仅更新记录，跳过安装与重启"
    log "========== 结束 =========="
    exit 1
fi

if [ "$(id -u)" -eq 0 ] || ! command -v sudo >/dev/null 2>&1; then
    SUDO=""
else
    SUDO="sudo"
fi

# REBOOT=1 → 通知 install_all.sh 跳过服务启动（只 enable），装完立刻回来重启
# 走命令行参数而不是环境变量，避免被 sudo 的 env_reset 清掉
INSTALL_ARGS=()
if [ "$REBOOT" = "1" ]; then
    PRE_REBOOT="${PRE_REBOOT:-1}"
else
    PRE_REBOOT="${PRE_REBOOT:-0}"
fi
[ "$PRE_REBOOT" = "1" ] && INSTALL_ARGS+=(--pre-reboot)

log ">>> 开始执行安装: $SUDO bash $INSTALL_SCRIPT ${INSTALL_ARGS[*]-}"

if command -v timeout >/dev/null 2>&1; then
    TMO="timeout -k 60 ${INSTALL_TIMEOUT}"
else
    TMO=""
fi

t0="$(date +%s)"
if [ "$FEED_Y" = "1" ]; then
    yes Y 2>/dev/null | $TMO $SUDO bash "$INSTALL_SCRIPT" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"} >>"$LOG_FILE" 2>&1 9>&-
    rc=${PIPESTATUS[2]:-${PIPESTATUS[1]}}
else
    $TMO $SUDO bash "$INSTALL_SCRIPT" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"} >>"$LOG_FILE" 2>&1 </dev/null 9>&-
    rc=$?
fi
INSTALL_COST=$(( $(date +%s) - t0 ))

if [ "$rc" -eq 0 ]; then
    log "安装脚本执行完成（退出码 0，耗时 ${INSTALL_COST}s，开机至今 $(uptime_s)s）"
else
    log "安装脚本退出码非 0: $rc（1=非交互下 read 到 EOF 或中途失败；124/137=超时被杀；耗时 ${INSTALL_COST}s）"
fi

if [ "$REBOOT" = "1" ]; then
    [ "${REBOOT_DELAY:-0}" -gt 0 ] 2>/dev/null && sleep "$REBOOT_DELAY"
    log ">>> 安装结束，开始重启设备（不询问、不等 Y）"
    reboot_now          # 优雅路径会在函数内 exit 0；返回即失败
    log "!! reboot_now 返回了，设备未能重启"
else
    log "REBOOT=0，按要求跳过重启"
fi

log "========== 结束（总耗时 $(elapsed)s）=========="
