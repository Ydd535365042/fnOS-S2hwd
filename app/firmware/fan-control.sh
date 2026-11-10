#!/bin/bash
# =============================================================
# Centerm NAS RK3568 风扇控制脚本（PWM7 占空比 4 锚点线性插值）
# 参考：https://zhuanlan.zhihu.com/p/5767112601
#
# 版本演进
#   v1  仅 CPU 温度分档
#   v2  温度滤波 / 去抖，避免风扇频繁启停
#   v3  新增【硬盘温度】参与调速
#   v4  预留页面手动调节接口（auto/manual 挡位）
#   v5  对齐 s2 fan-ui：三档策略（quiet/performance/custom）
#        - 取消 manual 固定挡位滑杆，全程温控
#        - 4 锚点温度 + 4 锚点转速百分比线性插值
#        - 硬盘路 / CPU 路各一条曲线，最终取较高转速百分比
#        - 起转 30% 固定，T4 全速 100% 固定
#
# 取温方式（按优先级）
#   1) drivetemp 内核模块 → /sys/class/hwmon  —— 纯内核态，不唤醒休眠硬盘，首选
#   2) smartctl -A -n standby                 —— 兜底；-n standby 保证不唤醒待机盘
#   3) 取不到硬盘温度 → 退回纯 CPU 温控（日志会提示）
# =============================================================

# ---------- 默认配置（可被 /etc/fan-control.conf 覆盖） ----------
USE_GPIO_FORCE=0     # 1=强制 GPIO；0=PWM 占空比（已确认有效）
PWM_CHIP=0
PWM_CHANNEL=0
PWM_PERIOD=40000     # 25kHz，period=40000ns
PWM_POLARITY="normal"
FAN_DUTY_MIN=30      # 起转占空比 %（T1 锚点下限）
FAN_GPIO=22          # GPIO 备用
CHECK_INTERVAL=3
FACTORY_TEST_CYCLE=10
TEMP_DEBOUNCE_CNT=2  # 连续 N 次目标占空比不同才真正调整

# ---- 硬盘路温控曲线（默认 quiet 预设，T4 天花板 55℃）----
HDD_T1=42
HDD_T2=48
HDD_T3=52
HDD_T4=55
HDD_D1=30            # T1 处转速 %（固定起转）
HDD_D2=30            # T2 处转速 %
HDD_D3=70            # T3 处转速 %
HDD_D4=100           # T4 处转速 %（固定全速）

# ---- CPU 路温控曲线（默认 quiet 预设）----
CPU_T1=60
CPU_T2=65
CPU_T3=72
CPU_T4=80
CPU_D1=30
CPU_D2=30
CPU_D3=70
CPU_D4=100

# 硬盘取温：hwmon 可用时不轮询 smartctl；不可用时每 N 个周期问一次
HDD_SMART_CYCLES=10  # 10 × 3s = 30s
HDD_SMART_TIMEOUT=8

# ---------- 运行态目录 ----------
RUN_DIR=/var/lib/fan-control
STATUS_FILE="$RUN_DIR/status"
# 页面「全速 10 秒」写这里：内容是到期的 epoch 秒。到期前无视温度、不去抖、直接 100%，
# 到期后自动回到温控。不改配置、不重启服务，所以测试结束不需要「还原曲线」。
FORCE_FILE="$RUN_DIR/force_until"

LOG_FILE="/var/log/fan_control.log"

# 载入外部配置（存在则覆盖上面的默认值）
CONF_FILE=/etc/fan-control.conf
[ -r "$CONF_FILE" ] && . "$CONF_FILE"

# 兜住 D1/D4：conf 里可能没写（前端固定 30/100）
[ -z "$HDD_D1" ] && HDD_D1=30
[ -z "$HDD_D4" ] && HDD_D4=100
[ -z "$CPU_D1" ] && CPU_D1=30
[ -z "$CPU_D4" ] && CPU_D4=100

# 页面切档 / 改曲线后写这个文件；主循环看到它就当场重读曲线、立刻按新曲线出风。
# 以前是每次 systemctl restart：要等 systemd 停 / 起服务，init_pwm 还会先把风扇关掉，
# 页面上点一下「性能」要等好几秒风扇才有反应，中间还会停转一下。
RELOAD_FILE="$RUN_DIR/reload"
CONF_MTIME=""

# 只重读曲线锚点（T1–T4 / D1–D4），PWM 参数改了才需要重启服务。
# 先读进临时变量、校验严格递增，坏配置（手改写错）就保留原曲线，不让风扇按一条歪曲线转。
reload_curve() {
    local f="$CONF_FILE" k v
    [ -r "$f" ] || return 1
    local -A n=()
    while IFS='=' read -r k v; do
        k="${k//[[:space:]]/}"; v="${v%%#*}"; v="${v//[[:space:]\"\']/}"
        case "$k" in
            HDD_T[1-4]|HDD_D[1-4]|CPU_T[1-4]|CPU_D[1-4])
                case "$v" in ''|*[!0-9]*) ;; *) n[$k]=$v ;; esac ;;
        esac
    done < "$f"
    local p
    # conf 里没写的锚点沿用当前值（手改时常只写一路曲线），合并之后再校验递增
    for k in HDD_T1 HDD_T2 HDD_T3 HDD_T4 HDD_D2 HDD_D3 CPU_T1 CPU_T2 CPU_T3 CPU_T4 CPU_D2 CPU_D3; do
        [ -n "${n[$k]:-}" ] || n[$k]="${!k}"
    done
    for p in HDD CPU; do
        local t1=${n[${p}_T1]:-} t2=${n[${p}_T2]:-} t3=${n[${p}_T3]:-} t4=${n[${p}_T4]:-}
        case "$t1$t2$t3$t4" in ''|*[!0-9]*) log "重读配置：${p} 曲线锚点不是数字，保留原曲线"; return 1 ;; esac
        [ "$t1" -lt "$t2" ] && [ "$t2" -lt "$t3" ] && [ "$t3" -lt "$t4" ] || { log "重读配置：${p} 锚点没有递增，保留原曲线"; return 1; }
    done
    for k in "${!n[@]}"; do printf -v "$k" '%s' "${n[$k]}"; done
    [ -z "$HDD_D1" ] && HDD_D1=30; [ -z "$HDD_D4" ] && HDD_D4=100
    [ -z "$CPU_D1" ] && CPU_D1=30; [ -z "$CPU_D4" ] && CPU_D4=100
    log "已重读曲线（不重启服务）硬盘 ${HDD_T1}/${HDD_T2}/${HDD_T3}/${HDD_T4}℃ ${HDD_D2}/${HDD_D3}%，CPU ${CPU_T1}/${CPU_T2}/${CPU_T3}/${CPU_T4}℃ ${CPU_D2}/${CPU_D3}%"
    return 0
}

# 配置文件指纹：纳秒级修改时间 + 大小 + inode（write_kv 是写临时文件再改名，inode 每次都变）
conf_mtime() {
    stat -c '%.9Y:%s:%i' "$CONF_FILE" 2>/dev/null && return
    stat -c '%Y:%s:%i' "$CONF_FILE" 2>/dev/null && return
    # 没有 stat（极简系统）：用文件内容本身做指纹，纯 bash 内建，不起进程
    local c=""; [ -r "$CONF_FILE" ] && c=$(<"$CONF_FILE"); echo "${#c}:${c//[[:space:]]/}"
}

mkdir -p "$RUN_DIR" 2>/dev/null

log() {
    local ts
    printf -v ts '%(%Y-%m-%d %H:%M:%S)T' -1
    echo "[$ts] $1" >> "$LOG_FILE"
}

# 只保留 1 天内的日志
trim_log() {
    [ -f "$LOG_FILE" ] || return 0
    local cutoff
    cutoff=$(date -d '1 day ago' '+%Y-%m-%d %H:%M' 2>/dev/null)
    [ -z "$cutoff" ] && return 0
    awk -v c="$cutoff" '/^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]/ {
        if (substr($0,2,16) >= c) print; next
    } {print}' "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE"
}

# ---------- PWM / GPIO ----------
find_pwm7_chip() {
    local chip path
    for chip in 0 1 2 3 4 5 6 7; do
        path="/sys/class/pwm/pwmchip${chip}"
        [ -d "$path" ] || continue
        if [ -e "${path}/device" ] && readlink -f "${path}/device" 2>/dev/null | grep -q "fe6e0030"; then
            echo $chip
            return 0
        fi
    done
    for chip in 0 1 2 3 4 5 6 7; do
        [ -d "/sys/class/pwm/pwmchip${chip}" ] || continue
        if echo $PWM_CHANNEL > /sys/class/pwm/pwmchip${chip}/export 2>/dev/null; then
            echo $chip
            return 0
        fi
    done
    echo ""
    return 1
}

init_pwm() {
    if [ -n "$PWM_CHIP" ] && [ -d "/sys/class/pwm/pwmchip${PWM_CHIP}" ]; then
        FOUND_CHIP=$PWM_CHIP
    else
        FOUND_CHIP=$(find_pwm7_chip)
    fi

    if [ -n "$FOUND_CHIP" ]; then
        PWM_PATH="/sys/class/pwm/pwmchip${FOUND_CHIP}/pwm${PWM_CHANNEL}"
        if [ ! -d "$PWM_PATH" ]; then
            echo $PWM_CHANNEL > /sys/class/pwm/pwmchip${FOUND_CHIP}/export 2>/dev/null
        fi
        if [ -d "$PWM_PATH" ]; then
            echo 0 > "${PWM_PATH}/enable" 2>/dev/null
            echo $PWM_PERIOD > "${PWM_PATH}/period" 2>/dev/null
            echo "$PWM_POLARITY" > "${PWM_PATH}/polarity" 2>/dev/null
            log "PWM 初始化成功 (pwmchip${FOUND_CHIP}, ${PWM_PERIOD}ns=25kHz, polarity=$PWM_POLARITY)"
            return 0
        fi
    fi
    log "PWM 不可用，将使用 GPIO${FAN_GPIO} 备用"
    return 1
}

init_gpio() {
    local gpio_path="/sys/class/gpio/gpio${FAN_GPIO}"
    if [ -d "$gpio_path" ]; then
        log "GPIO${FAN_GPIO} 已存在，直接使用"
    else
        echo $FAN_GPIO > /sys/class/gpio/export 2>/dev/null
    fi
    echo out > "${gpio_path}/direction" 2>/dev/null
    echo 0 > "${gpio_path}/value" 2>/dev/null
}

# duty_pct: 0-100 整数。0=停转，>=FAN_DUTY_MIN 才真正输出占空比。
set_fan_speed_pwm() {
    local pct=$1
    local duty=0
    if [ "$pct" -gt 0 ]; then
        [ "$pct" -lt "$FAN_DUTY_MIN" ] && pct=$FAN_DUTY_MIN
        [ "$pct" -gt 100 ] && pct=100
        if [ "$PWM_POLARITY" = "inversed" ]; then
            duty=$(( PWM_PERIOD * (100 - pct) / 100 ))
        else
            duty=$(( PWM_PERIOD * pct / 100 ))
        fi
        echo $duty > "${PWM_PATH}/duty_cycle" 2>/dev/null
        echo 1 > "${PWM_PATH}/enable" 2>/dev/null
    else
        echo 0 > "${PWM_PATH}/enable" 2>/dev/null
    fi
    LAST_DUTY=$pct
}

# GPIO 备用：无法调占空比，>0 即全速 100%
set_fan_speed_gpio() {
    local pct=$1
    local gpio_path="/sys/class/gpio/gpio${FAN_GPIO}"
    if [ "$pct" -gt 0 ]; then
        echo 1 > "${gpio_path}/value" 2>/dev/null
        LAST_DUTY=100
    else
        echo 0 > "${gpio_path}/value" 2>/dev/null
        LAST_DUTY=0
    fi
}

# ---------- 温度采集 ----------
get_cpu_temp() {
    local temp_raw
    read -r temp_raw 2>/dev/null < /sys/class/thermal/thermal_zone0/temp || temp_raw=0
    [ -n "$temp_raw" ] || temp_raw=0
    echo $(( temp_raw / 1000 ))
}

# 列出所有物理磁盘（含 NVMe）
list_disks() {
    local d out=""
    for d in /dev/sd[a-z] /dev/nvme[0-9]n[0-9]; do
        [ -b "$d" ] && out="$out $d"
    done
    echo $out
}

# 方式1：drivetemp 内核模块（不唤醒硬盘）。
# 输出格式：每行 "名字=温度"
read_disk_temp_hwmon() {
    local d n v name t blk
    for d in /sys/class/hwmon/hwmon*; do
        [ -r "$d/name" ] || continue
        read -r n 2>/dev/null < "$d/name" || n=""
        case "$n" in
            drivetemp) ;;
            *) continue ;;
        esac
        read -r v 2>/dev/null < "$d/temp1_input" || continue
        [ -n "$v" ] || continue
        name=""
        for blk in "${d}/device/block"/*; do
            [ -e "$blk" ] && { name="${blk##*/}"; break; }
        done
        [ -n "$name" ] || name="${d##*/}"
        t=$(( v / 1000 ))
        echo "${name}=${t}"
    done
}

# 方式2：smartctl（兜底）。-n standby 保证不会唤醒待机盘。
read_disk_temp_smart() {
    local dev t name out=""
    for dev in $(list_disks); do
        name="${dev##*/}"
        case "$name" in
            nvme*)
                t=$(timeout "$HDD_SMART_TIMEOUT" smartctl -A -n standby "$dev" 2>/dev/null \
                    | awk '/^[Tt]emperature[:/]/ {for(i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) {print $i; exit}}')
                ;;
            *)
                t=$(timeout "$HDD_SMART_TIMEOUT" smartctl -A -n standby "$dev" 2>/dev/null \
                    | awk '/^ *(190|194) / {print $10; exit}')
                ;;
        esac
        case "${t:-}" in
            ''|*[!0-9]*) continue ;;
        esac
        [ "$t" -gt 120 ] && continue
        out="$out ${name}=${t}"
    done
    echo $out
}

# ---------- 4 锚点线性插值 ----------
# 参数: $1=temp $2=t1 $3=t2 $4=t3 $5=t4 $6=d1 $7=d2 $8=d3 $9=d4
# 输出: 0-100 整数占空比
# 语义: temp < t1 → 0%（停转）；t1 处阶跃到 d1%（起转）；
#       t1~t2 / t2~t3 / t3~t4 三段线性插值；temp >= t4 → d4%（全速）
curve_duty() {
    local temp=$1 t1=$2 t2=$3 t3=$4 t4=$5 d1=$6 d2=$7 d3=$8 d4=$9
    local r
    case "${t1}${t2}${t3}${t4}" in *[!0-9]*) echo "$d1"; return ;; esac
    if   [ "$temp" -lt  "$t1" ]; then echo 0
    elif [ "$temp" -ge "$t4" ]; then echo "$d4"
    elif [ "$temp" -lt  "$t2" ]; then
        r=$(( t2 - t1 )); [ "$r" -le 0 ] && { echo "$d2"; return; }
        echo $(( d1 + (temp - t1) * (d2 - d1) / r ))
    elif [ "$temp" -lt  "$t3" ]; then
        r=$(( t3 - t2 )); [ "$r" -le 0 ] && { echo "$d3"; return; }
        echo $(( d2 + (temp - t2) * (d3 - d2) / r ))
    else
        r=$(( t4 - t3 )); [ "$r" -le 0 ] && { echo "$d4"; return; }
        echo $(( d3 + (temp - t3) * (d4 - d3) / r ))
    fi
}

# ---------- 状态输出（供页面 / fan-api 读取） ----------
write_status() {
    local duty=$1 cpu=$2 hddmax=$3 src=$4 disks=$5 mode=${6:-auto}
    local ts
    printf -v ts '%(%Y-%m-%d %H:%M:%S)T' -1
    {
        echo "timestamp=$ts"
        echo "mode=$mode"
        echo "guard_reason=$guard_reason"
        echo "level=$duty"
        echo "duty_pct=$duty"
        echo "cpu_temp=$cpu"
        echo "hdd_max_temp=$hddmax"
        echo "hdd_source=$src"
        echo "disks=$disks"
    } > "${STATUS_FILE}.tmp" 2>/dev/null && mv "${STATUS_FILE}.tmp" "$STATUS_FILE" 2>/dev/null
}

# 独立于网页进程的保护策略；只解析固定数值字段，绝不 source 配置。
GUARD_CONF="${S2_GUARD_CONF:-/etc/s2hwctl/fan-guard.conf}"
guard_active=0; guard_bad=0; guard_good=0; guard_reason=""
check_guard() {
    local cpu=$1 disk=$2 enabled=0 cpu_limit=80 disk_limit=55 fail_samples=3 k v reason=""
    if [ -r "$GUARD_CONF" ]; then
        while IFS='=' read -r k v; do
            case "$v" in ''|*[!0-9]*) continue ;; esac
            case "$k" in
                ENABLED) enabled=$v ;;
                CPU_LIMIT) [ "$v" -ge 50 ] && [ "$v" -le 85 ] && cpu_limit=$v ;;
                DISK_LIMIT) [ "$v" -ge 40 ] && [ "$v" -le 60 ] && disk_limit=$v ;;
                FAIL_SAMPLES) [ "$v" -ge 1 ] && [ "$v" -le 10 ] && fail_samples=$v ;;
            esac
        done < "$GUARD_CONF"
    fi
    if [ "$enabled" != 1 ]; then
        guard_active=0; guard_bad=0; guard_good=0; guard_reason=""; return
    fi
    if [ "$cpu" -le 0 ] || [ "$cpu" -gt 125 ]; then reason="CPU 温度传感器无效"
    elif [ "$cpu" -ge "$cpu_limit" ]; then reason="CPU 持续超温"
    elif [ "$disk" -ge "$disk_limit" ]; then reason="硬盘持续超温"; fi
    if [ -n "$reason" ]; then
        guard_bad=$((guard_bad + 1)); guard_good=0
        if [ "$guard_bad" -ge "$fail_samples" ]; then
            [ "$guard_active" -eq 0 ] && log "风扇保护启用：$reason"
            guard_active=1; guard_reason="$reason"
        fi
    elif [ "$cpu" -lt $((cpu_limit - 3)) ] && [ "$disk" -lt $((disk_limit - 3)) ]; then
        guard_bad=0; guard_good=$((guard_good + 1))
        if [ "$guard_good" -ge 2 ]; then
            [ "$guard_active" -eq 1 ] && log "风扇保护恢复：传感器正常且温度下降"
            guard_active=0; guard_reason=""
        fi
    fi
}

# ---------- 主循环 ----------
main() {
    trim_log
    log "=== 风扇控制服务启动（v5：三档策略 + 4 锚点百分比线性插值） ==="
    log "曲线 硬盘: T1=${HDD_T1}°/${HDD_D1}% T2=${HDD_T2}°/${HDD_D2}% T3=${HDD_T3}°/${HDD_D3}% T4=${HDD_T4}°/${HDD_D4}%"
    log "曲线 CPU : T1=${CPU_T1}°/${CPU_D1}% T2=${CPU_T2}°/${CPU_D2}% T3=${CPU_T3}°/${CPU_D3}% T4=${CPU_T4}°/${CPU_D4}%"

    modprobe drivetemp 2>/dev/null && log "drivetemp 模块已加载（硬盘温度走内核 hwmon，不唤醒硬盘）"

    local use_pwm=1
    if [ $USE_GPIO_FORCE -eq 1 ]; then
        use_pwm=0
        init_gpio
    else
        if ! init_pwm; then
            use_pwm=0
            init_gpio
        fi
    fi

    local cycle_count=0
    local debounce_cnt=0
    local last_duty=-1
    LAST_DUTY=0
    local cache_disks=""
    local hdd_source="none"
    local forcing=0
    rm -f "$FORCE_FILE" "$RELOAD_FILE" 2>/dev/null   # 上次测试没跑完就重启/断电：别带着全速开机
    CONF_MTIME=$(conf_mtime)

    while true; do
        local cpu hdd_max=0 hdd_duty=0 cpu_duty=0 target_duty=0
        local d t name

        # ---- 页面改了曲线：当场重读，跳过去抖立刻生效 ----
        # 看两样：页面写的 reload 标记（立即），或配置文件 mtime 变了（有人手改 conf）
        # 比较的是「文件指纹」（修改时间 + 大小 + inode），同一秒里写两次也分得出
        local mt
        mt=$(conf_mtime)
        if [ -e "$RELOAD_FILE" ] || { [ -n "$mt" ] && [ "$mt" != "$CONF_MTIME" ]; }; then
            rm -f "$RELOAD_FILE" 2>/dev/null
            CONF_MTIME="$mt"                  # 坏配置也记下指纹：不改就不每轮重试、刷屏
            reload_curve && last_duty=-1      # -1：下面按新曲线直接落一次，不等两轮去抖
        fi

        cpu=$(get_cpu_temp)

        # ---- 硬盘温度：hwmon 优先，否则按周期轮询 smartctl ----
        local disk_snap=""
        if [ -d /sys/class/hwmon ]; then
            disk_snap=$(read_disk_temp_hwmon)
        fi
        if [ -n "$disk_snap" ]; then
            hdd_source="hwmon"
            cache_disks="$disk_snap"
        else
            if [ $((cycle_count % HDD_SMART_CYCLES)) -eq 0 ] || [ -z "$cache_disks" ]; then
                local s
                s=$(read_disk_temp_smart)
                if [ -n "$s" ]; then
                    hdd_source="smartctl"
                    cache_disks="$s"
                fi
            fi
            disk_snap="$cache_disks"
        fi

        # 取所有盘的最高温度
        for d in $disk_snap; do
            name="${d%%=*}"
            t="${d##*=}"
            case "${t:-}" in ''|*[!0-9]*) continue ;; esac
            [ "$t" -gt "$hdd_max" ] && hdd_max=$t
        done

        # ---- 计算目标占空比：两路曲线取较高者 ----
        if [ "$hdd_max" -gt 0 ]; then
            hdd_duty=$(curve_duty "$hdd_max" \
                "$HDD_T1" "$HDD_T2" "$HDD_T3" "$HDD_T4" \
                "$HDD_D1" "$HDD_D2" "$HDD_D3" "$HDD_D4")
            [ "$hdd_max" -ge "$HDD_T4" ] && log "⚠️  硬盘温度 ${hdd_max}℃ 已达上限阈值 ${HDD_T4}℃，风扇全速"
        else
            hdd_duty=0
        fi
        cpu_duty=$(curve_duty "$cpu" \
            "$CPU_T1" "$CPU_T2" "$CPU_T3" "$CPU_T4" \
            "$CPU_D1" "$CPU_D2" "$CPU_D3" "$CPU_D4")
        target_duty=$(( hdd_duty > cpu_duty ? hdd_duty : cpu_duty ))
        [ "$target_duty" -gt 100 ] && target_duty=100

        # ---- 页面全速测试 ----
        local now force_until="" mode=auto
        printf -v now '%(%s)T' -1
        [ -r "$FORCE_FILE" ] && read -r force_until 2>/dev/null < "$FORCE_FILE"
        case "${force_until:-}" in ''|*[!0-9]*) force_until=0 ;; esac
        if [ "$force_until" -gt "$now" ]; then
            [ "$forcing" -eq 0 ] && log "页面全速测试：风扇 100%，$(( force_until - now )) 秒后回到温控"
            forcing=1
            mode=test
            target_duty=100
        elif [ "$forcing" -eq 1 ]; then
            forcing=0
            last_duty=-1          # 测试结束：下面直接按当前温度落一次，不去抖
            log "全速测试结束，回到温控"
        fi

        local was_guard=$guard_active
        check_guard "$cpu" "$hdd_max"
        if [ "$guard_active" -eq 1 ]; then
            target_duty=100; mode=protect
        elif [ "$was_guard" -eq 1 ]; then
            last_duty=-1
        fi

        # ---- 去抖 ----
        # last_duty=-1：刚启动 / 测试刚结束。init_pwm 把风扇关了，这时再等两轮去抖
        # 就是白白停转 6 秒（每次在页面上保存曲线都会重启服务），所以立刻生效。
        if [ "$target_duty" -ne "$last_duty" ] && { [ "$forcing" -eq 1 ] || [ "$guard_active" -eq 1 ] || [ "$last_duty" -lt 0 ]; }; then
            last_duty=$target_duty
            debounce_cnt=0
            if [ $use_pwm -eq 1 ]; then set_fan_speed_pwm "$last_duty"; else set_fan_speed_gpio "$last_duty"; fi
            log "CPU ${cpu}℃ / 硬盘最高 ${hdd_max}℃($hdd_source) → 占空比 ${last_duty}% (硬盘${hdd_duty}% CPU${cpu_duty}%)"
        elif [ "$target_duty" -ne "$last_duty" ]; then
            if [ "$debounce_cnt" -lt "$TEMP_DEBOUNCE_CNT" ]; then
                debounce_cnt=$((debounce_cnt + 1))
            else
                last_duty=$target_duty
                debounce_cnt=0
                if [ $use_pwm -eq 1 ]; then set_fan_speed_pwm "$last_duty"; else set_fan_speed_gpio "$last_duty"; fi
                log "CPU ${cpu}℃ / 硬盘最高 ${hdd_max}℃($hdd_source) → 占空比 ${last_duty}% (硬盘${hdd_duty}% CPU${cpu_duty}%)"
            fi
        else
            debounce_cnt=0
        fi

        write_status "$last_duty" "$cpu" "$hdd_max" "$hdd_source" "$disk_snap" "$mode"

        cycle_count=$((cycle_count + 1))
        if [ $((cycle_count % FACTORY_TEST_CYCLE)) -eq 0 ]; then
            log "心跳: CPU=${cpu}℃ 硬盘最高=${hdd_max}℃($hdd_source) 占空比=${last_duty}%"
            trim_log
        fi

        # 等下一轮：拆成 0.25 秒一小段，期间页面一写 reload / force_until 就提前醒，
        # 切档、全速测试不用等满一个 CHECK_INTERVAL（3 秒）
        local w=0 steps=$(( CHECK_INTERVAL * 4 ))
        while [ "$w" -lt "$steps" ]; do
            sleep 0.25 2>/dev/null || { sleep 1; w=$((w + 3)); }
            [ -e "$RELOAD_FILE" ] && break
            if [ "$forcing" -eq 0 ] && [ -s "$FORCE_FILE" ]; then break; fi
            w=$((w + 1))
        done
    done
}

main
