#!/bin/bash
# =============================================================
# selfheal-check.sh —— 定制功能自检 + 自愈
#
# 为什么需要它
# ------------
# 飞牛走 apt 升级，内核包 linux-image-*-trim 自带
#   /usr/lib/linux-image-*/rockchip/rk3568-nanopi-r5s.dtb
# 升级时会把它装回 /boot/dtb/rockchip/ —— 我们的定制 DTB 就被官方版覆盖了
# （后果：SATA 口回到 disabled、LED/风扇/蜂鸣器 GPIO 失效）。
#
# 原本靠 boot-version-guard.sh 兜底，但它靠 /etc/issue 里的 "OS version"
# 这一行来判断版本变化：飞牛只要改写法（比如改成 "Version:"），它就永远
# 不触发，DTB 再也没人装回来。
#
# 所以本脚本【不猜版本号】，直接看东西在不在、对不对：
#   1) /boot/dtb/rockchip/rk3568-nanopi-r5s.dtb 的 MD5 是否等于定制版
#   2) 关键脚本 / service / logind 配置是否缺失
#   3) /usr/local/share/install-1014 安装源是否还在（不在就从 /usr/local/share 备份恢复）
# 有问题 → 重装 → 重启生效。带连续重试上限，绝不会无限重启。
#
# 用法
# ----
#   selfheal-check.sh               # 开机模式：修复后重启
#   selfheal-check.sh --no-reboot   # 定时任务模式：只修复，不打扰正在用的机器
# =============================================================
set -uo pipefail

LOG_TAG=selfheal
BACKUP_DIR=/usr/local/share/install-1014      # apt 升级不会碰这里
INSTALL_DIR=/usr/local/share/install-1014           # guard 硬编码找这里
INSTALL_SCRIPT="$INSTALL_DIR/install_all.sh"
GUARD_BIN=/usr/local/bin/boot-version-guard.sh
DTB_DST=/boot/dtb/rockchip/rk3568-nanopi-r5s.dtb
DTB_REF="$BACKUP_DIR/rk3568-nanopi-r5s-new.dtb"

STATE_DIR=/var/lib/selfheal
STATE_FILE="$STATE_DIR/retry_count"
MAX_RETRY=3

NO_REBOOT=0
for a in "$@"; do
    [ "$a" = "--no-reboot" ] && NO_REBOOT=1
done

mkdir -p "$STATE_DIR"

log() {
    logger -t "$LOG_TAG" "$*" 2>/dev/null
    echo "[selfheal] $*"
}

need_fix=0
reason=""

# ---------- 1) 安装源还在吗？不在就从备份恢复 ----------
if [ ! -f "$INSTALL_SCRIPT" ]; then
    if [ -f "$BACKUP_DIR/install_all.sh" ]; then
        log "install source gone -> restore from $BACKUP_DIR"
        mkdir -p "$INSTALL_DIR"
        cp -a "$BACKUP_DIR/." "$INSTALL_DIR/" 2>/dev/null
        need_fix=1
        reason="${reason}install-source-restored "
    else
        log "FATAL: both $INSTALL_SCRIPT and $BACKUP_DIR missing"
    fi
fi

# ---------- 2) DTB 指纹校验（核心） ----------
if [ -f "$DTB_REF" ]; then
    ref_md5=$(md5sum "$DTB_REF" 2>/dev/null | awk '{print $1}')
    cur_md5=$(md5sum "$DTB_DST" 2>/dev/null | awk '{print $1}')
    if [ -n "$ref_md5" ] && [ "$ref_md5" != "$cur_md5" ]; then
        log "DTB mismatch: boot=${cur_md5:-none} ref=$ref_md5"
        need_fix=1
        reason="${reason}dtb-mismatch "
    fi
else
    log "WARN: no DTB reference at $DTB_REF, skip DTB check"
fi

# ---------- 3) 关键文件是否齐全 ----------
KEY_FILES="
/usr/local/sbin/power-key-daemon.py
/usr/local/sbin/random-mac.sh
/usr/local/sbin/beep-short.sh
/usr/local/sbin/power-led-solid.sh
/usr/local/sbin/power-led-net.sh
/etc/systemd/system/power-led-net.service
/usr/local/sbin/sata-led-enable.sh
/usr/local/sbin/sata-led-manager.sh
/usr/local/sbin/set-hostname.sh
/etc/sata-led.conf
/usr/local/bin/fan-control.sh
/usr/local/bin/fanctl
/etc/fan-control.conf
/usr/local/sbin/fan-api.py
/usr/bin/power-key.sh
/etc/systemd/logind.conf.d/10-power-key.conf
/etc/systemd/system/power-key-daemon.service
/etc/systemd/system/random-mac.service
/etc/systemd/system/fan-control.service
"
for f in $KEY_FILES; do
    if [ ! -f "$f" ]; then
        log "missing: $f"
        need_fix=1
        reason="${reason}missing:$f "
    fi
done

# ---------- 判定 ----------
if [ "$need_fix" -eq 0 ]; then
    echo 0 > "$STATE_FILE"
    log "OK — everything in place"
    exit 0
fi

count=$(cat "$STATE_FILE" 2>/dev/null)
[ -z "$count" ] && count=0
count=$((count + 1))
echo "$count" > "$STATE_FILE"

if [ "$count" -gt "$MAX_RETRY" ]; then
    log "GIVE UP after $MAX_RETRY attempts (reason: $reason)"
    log "请手动执行： bash $INSTALL_SCRIPT"
    exit 1
fi

log "fix attempt #$count, reason: $reason"

# ---------- 修复 ----------
if [ ! -f "$INSTALL_SCRIPT" ]; then
    log "install script unavailable — abort"
    exit 1
fi

chmod +x "$INSTALL_SCRIPT" 2>/dev/null
bash "$INSTALL_SCRIPT" --pre-reboot >>/var/log/selfheal-install.log 2>&1
log "install_all.sh rc=$?"

# 已经重装过了，顺手把 guard 的基线刷新成当前版本，
# 免得它随后又判"版本变了"再重装一次、多重启一轮
if [ -x "$GUARD_BIN" ]; then
    "$GUARD_BIN" --init >/dev/null 2>&1 \
        && log "guard baseline refreshed" \
        || log "guard baseline refresh skipped (no OS version line yet)"
fi

if [ "$NO_REBOOT" -eq 1 ]; then
    log "no-reboot mode: 已修复，下次重启后生效"
    exit 0
fi

log "reboot to apply DTB (fix #$count)"
sync
# 异步重启：本脚本立即退出，别把 systemd 的关机流程卡住
nohup bash -c 'sleep 3; systemctl --no-block reboot 2>/dev/null || reboot -f' >/dev/null 2>&1 &
exit 0
