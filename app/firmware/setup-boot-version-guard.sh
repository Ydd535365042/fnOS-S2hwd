#!/usr/bin/env bash
# =============================================================
# 一键部署：开机版本守护（systemd 方案）  —— v4
# 在 fnOS 上以 root 执行： sudo bash setup-boot-version-guard.sh
#
# v3 要点：
#   1) service 文件缺失时内置生成，不再因 set -e 让整条部署链断开
#   2) 部署后自检关键属性（TimeoutStartSec / KillMode / Environment）
#   3) 打开并持久化 kernel.sysrq，保证最后一级兜底可用
#   4) 部署完打印可复制的诊断命令
#
# v4 要点（快重启）：
#   1) 【最大的一处提速】内置 unit 去掉 Wants/After=network-online.target。
#      guard 只读 /etc/issue、跑本地脚本，根本不需要网络；
#      而 network-online 在 DHCP 慢 / 无网时会白等 30~90s ——
#      这才是"设备开机后迟迟不重启"的最大元凶，比 BOOT_DELAY 严重得多。
#      改为 After=local-fs.target + RequiresMountsFor=<安装脚本所在目录>，
#      精确等到 /home（若独立分区）挂好即可，不等网络。
#   2) 若你提供了自己的 boot-version-guard.service，本脚本会检查其中是否仍
#      含 network-online.target 并给出警告（旧 unit 会把提速效果抵消掉）。
#   3) 把 BOOT_DELAY / REBOOT_DELAY / SCHED_WAIT / INSTALL_TIMEOUT 写进 unit 的
#      Environment，默认 3 / 1 / 12 / 900，无需再改脚本本体即可调参。
#   4) sudoers 增加 env_keep，方便手动调试时传 PRE_REBOOT 等变量。
# =============================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD_BIN="/usr/local/bin/boot-version-guard.sh"
SERVICE="/etc/systemd/system/boot-version-guard.service"
SUDOERS="/etc/sudoers.d/boot-version-guard"
SYSCTL="/etc/sysctl.d/99-boot-version-guard.conf"
INSTALL_SCRIPT="/usr/local/share/install-1014/install_all.sh"
INSTALL_DIR="$(dirname "$INSTALL_SCRIPT")"

# 默认参数（会写进 unit 的 Environment，改这里即可，无需手改 guard 脚本）
BOOT_DELAY="${BOOT_DELAY:-3}"
REBOOT_DELAY="${REBOOT_DELAY:-1}"
SCHED_WAIT="${SCHED_WAIT:-12}"
INSTALL_TIMEOUT="${INSTALL_TIMEOUT:-900}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 执行： sudo bash $0"; exit 1
fi

# 1. 主脚本
if [ ! -f "$SRC_DIR/boot-version-guard.sh" ]; then
    echo "[error] 同目录下缺少 boot-version-guard.sh： $SRC_DIR"
    exit 1
fi
install -m 0755 "$SRC_DIR/boot-version-guard.sh" "$GUARD_BIN"
echo "[ok] 主脚本 -> $GUARD_BIN"

# 1.5 部署即刻记录当前版本作为基线（不执行安装、不重启）
echo
echo "--- 记录当前版本基线 ---"
if "$GUARD_BIN" --init; then
    echo "[ok] 当前版本已写入基线"
else
    echo "[warn] 基线写入失败，请检查 /etc/issue 是否包含 'OS version' 行"
fi
echo

# 2. 校验安装脚本存在（不存在也允许部署，主脚本会自动跳过安装）
if [ -f "$INSTALL_SCRIPT" ]; then
    chmod +x "$INSTALL_SCRIPT" 2>/dev/null || true
    echo "[ok] 安装脚本存在: $INSTALL_SCRIPT"
else
    echo "[warn] 未找到 $INSTALL_SCRIPT，请确认路径或修改 $GUARD_BIN 里的 INSTALL_SCRIPT"
fi

# 3. sudoers：允许免密执行（手动调试 / cron 方案会用到）
mkdir -p "$(dirname "$SUDOERS")"
cat >"$SUDOERS" <<EOF
# 允许 anna 免密执行版本守护与安装脚本
anna ALL=(ALL) NOPASSWD: $GUARD_BIN, /usr/bin/bash $INSTALL_SCRIPT, /sbin/reboot
# 手动调试时保留 PRE_REBOOT 等变量（正常由 systemd 运行时不受影响）
Defaults:anna env_keep += "PRE_REBOOT BOOT_DELAY REBOOT_DELAY SCHED_WAIT INSTALL_TIMEOUT"
EOF
chmod 0440 "$SUDOERS"
if command -v visudo >/dev/null 2>&1; then
    if visudo -cf "$SUDOERS" >/dev/null 2>&1; then
        echo "[ok] sudoers -> $SUDOERS（校验通过）"
    else
        echo "[warn] sudoers 校验未通过，请手动检查: $SUDOERS"
    fi
else
    echo "[ok] sudoers -> $SUDOERS（未找到 visudo，跳过校验）"
fi

# 4. 打开 Magic SysRq，保证最后一级兜底重启可用
echo 'kernel.sysrq=1' >"$SYSCTL"
if sysctl -w kernel.sysrq=1 >/dev/null 2>&1; then
    echo "[ok] kernel.sysrq=1（sysrq 兜底已启用并持久化到 $SYSCTL）"
else
    echo "[warn] kernel.sysrq 设置失败；请确认内核开启了 CONFIG_MAGIC_SYSRQ（不影响前两级重启）"
fi
[ -e /proc/sysrq-trigger ] && echo "[ok] /proc/sysrq-trigger 存在" \
    || echo "[warn] /proc/sysrq-trigger 不存在，sysrq 兜底不可用（不影响前两级重启）"

# 5. systemd 服务
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    USE_SRC=0
    if [ -f "$SRC_DIR/boot-version-guard.service" ]; then
        # 旧 unit 若还挂着 network-online，会把 v4 的提速效果抵消 —— 必须提醒
        if grep -q 'network-online' "$SRC_DIR/boot-version-guard.service" 2>/dev/null; then
            echo
            echo "[warn] 你自带的 boot-version-guard.service 含 network-online.target，"
            echo "       guard 不需要网络，这会让它一直等到网络就绪才启动（慢 30~90s）。"
            echo "       建议删除该 unit 后重新运行本脚本，使用内置版本。"
        fi
        install -m 0644 "$SRC_DIR/boot-version-guard.service" "$SERVICE"
        USE_SRC=1
        echo "[ok] 服务文件 -> $SERVICE（来自 $SRC_DIR）"
    else
        echo "[warn] $SRC_DIR 下没有 boot-version-guard.service，使用内置版本生成"
    fi

    if [ "$USE_SRC" -eq 0 ]; then
        cat >"$SERVICE" <<UNIT
[Unit]
Description=Boot version guard: reinstall & reboot on fnOS/kernel version change
After=local-fs.target
# 不等 network-online：guard 只读 /etc/issue 并跑本地脚本，与网络无关
# 只精确等待安装脚本所在目录挂载完成（/home 若独立分区则等它，否则无操作）
RequiresMountsFor=${INSTALL_DIR}

[Service]
Type=oneshot
ExecStart=/usr/local/bin/boot-version-guard.sh
TimeoutStartSec=infinity
KillMode=none
TimeoutStopSec=5
TimeoutStopFailureMode=terminate
Environment=BOOT_DELAY=${BOOT_DELAY}
Environment=REBOOT_DELAY=${REBOOT_DELAY}
Environment=SCHED_WAIT=${SCHED_WAIT}
Environment=INSTALL_TIMEOUT=${INSTALL_TIMEOUT}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
        chmod 0644 "$SERVICE"
        echo "[ok] 服务文件 -> $SERVICE（内置版本，不等 network-online）"
    fi

    timeout 60 systemctl daemon-reload
    timeout 60 systemctl enable boot-version-guard.service
    echo "[ok] 已启用开机服务 boot-version-guard.service"

    echo
    echo "--- 部署自检（前两项必须是 infinity / none；Environment 应含新的四个默认值）---"
    systemctl show boot-version-guard \
        -p FragmentPath -p TimeoutStartUSec -p KillMode -p Environment \
        -p After -p RequiresMountsFor 2>/dev/null | sed 's/^/    /'
    echo
    echo "--- 网络等待自检（应为空，若出现 network-online 说明还在等网络）---"
    systemctl show boot-version-guard -p After -p Wants -p Requires 2>/dev/null \
        | grep -o 'network-online[^ ]*' | sed 's/^/    [still waiting] /' \
        || echo "    ✅ 未依赖 network-online"
else
    echo "[warn] 未检测到 systemd，请改用 cron 方案："
    echo "  ( crontab -l 2>/dev/null; echo '@reboot sleep 15 && $GUARD_BIN' ) | crontab -"
fi

cat <<EOF

--- 常用命令 ---
  systemctl status  boot-version-guard          # 看上次执行结果
  systemctl start   boot-version-guard          # 立即手动跑一次
  systemctl disable boot-version-guard          # 取消开机自启
  tail -f /var/log/boot-version-guard.log       # 看日志
  $GUARD_BIN --init                             # 手动刷新基线为当前版本
  $GUARD_BIN --test-reboot                      # 只测重启链路（会真重启！）

EOF

echo
echo "基线文件： /var/lib/boot-version-guard/last_version"
echo "当前基线： $(cat /var/lib/boot-version-guard/last_version 2>/dev/null || echo '(无)')"
echo
echo "部署完成。下次开机起，若版本与该基线不一致才执行安装并重启。"
