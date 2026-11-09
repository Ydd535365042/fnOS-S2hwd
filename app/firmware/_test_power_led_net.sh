#!/usr/bin/env bash
# 离线自测：用 fake sysfs 验证 power-led-net.sh 的灯态
#   有链路 → 绿灯常亮；无链路 → 红灯闪烁
set -u
SRC="D:/S2-FNOS/zjtfnos/install-1014/install-1014/power-led-net.sh"
RT=/tmp/plednet_test
rm -rf "$RT"
mkdir -p "$RT/leds/power:green" "$RT/leds/power:red" "$RT/net/eth0" "$RT/net/eth1" "$RT/net/lo"

mkled() { mkdir -p "$1"; echo 0 > "$1/brightness"; echo "none" > "$1/trigger"; }
mkled "$RT/leds/power:green"; mkled "$RT/leds/power:red"
echo 0 > "$RT/net/eth0/carrier"; echo 0 > "$RT/net/eth1/carrier"

mkconf() {  # $1=输出文件  $2=额外行(可空)
  cat > "$1" <<EOF
LEDS_ROOT=$RT/leds
NET_ROOT=$RT/net
LOG_FILE=$RT/pled.log
LOG_ENABLE=1
TICK=0.1
CHECK_EVERY=5
BLINK_ON_TICKS=2
BLINK_OFF_TICKS=2
EOF
  [ -n "${2:-}" ] && echo "$2" >> "$1"
  printf '%s\n' "$1"
}

G() { cat "$RT/leds/power:green/brightness" 2>/dev/null || echo "-"; }
R() { cat "$RT/leds/power:red/brightness"   2>/dev/null || echo "-"; }
expect() { local d="$1" w="$2" g="$3"; if [ "$w" = "$g" ]; then echo "  PASS  $d"; else echo "  FAIL  $d  期望[$w] 实际[$g]"; fi; }
run_bg() { POWER_LED_NET_CONF="$1" bash "$SRC" >/dev/null 2>&1 & echo $!; }
sample_red() {  # 采样 N 次红灯值，返回去重后的序列
  local s="" i
  for i in $(seq 1 12); do s="$s$(R)"; sleep 0.05; done
  echo "$s"
}

echo "== 场景1：两个网口都没插网线（carrier=0）=="
C=$(mkconf "$RT/c1.conf")
P=$(run_bg "$C"); sleep 1; SR=$(sample_red); kill $P 2>/dev/null; wait $P 2>/dev/null
echo "  红灯采样序列: $SR"
expect "无链路 → 绿灯灭" "0" "$(G)"
case "$SR" in *1*) echo "  PASS  无链路 → 红灯出现高电平（闪烁中）";; *) echo "  FAIL  无链路 → 红灯始终为 0，没闪";; esac
case "$SR" in *0*) echo "  PASS  无链路 → 红灯出现低电平（确为闪烁而非常亮）";; *) echo "  FAIL  红灯常亮，不是闪烁";; esac

echo "== 场景2：eth0 插上网线（carrier=1）=="
echo 1 > "$RT/net/eth0/carrier"
C=$(mkconf "$RT/c2.conf")
P=$(run_bg "$C"); sleep 1; SR=$(sample_red); kill $P 2>/dev/null; wait $P 2>/dev/null
expect "有链路 → 绿灯常亮" "1" "$(G)"
expect "有链路 → 红灯灭" "0" "$(R)"

echo "== 场景3：拔掉网线（carrier 回 0）=="
echo 0 > "$RT/net/eth0/carrier"
C=$(mkconf "$RT/c3.conf")
P=$(run_bg "$C"); sleep 1; SR=$(sample_red); kill $P 2>/dev/null; wait $P 2>/dev/null
echo "  红灯采样序列: $SR"
expect "拔线 → 绿灯灭" "0" "$(G)"
case "$SR" in *1*0*|*0*1*) echo "  PASS  拔线 → 红灯闪烁";; *) echo "  FAIL  拔线 → 红灯不闪（序列 $SR）";; esac

echo "== 场景4：只有 eth1 插线（另一个口）=="
echo 1 > "$RT/net/eth1/carrier"
C=$(mkconf "$RT/c4.conf")
P=$(run_bg "$C"); sleep 1; kill $P 2>/dev/null; wait $P 2>/dev/null
expect "eth1 有链路 → 绿灯常亮" "1" "$(G)"
expect "eth1 有链路 → 红灯灭" "0" "$(R)"
echo 0 > "$RT/net/eth1/carrier"

echo "== 场景5：单色灯机器（只有 power:green，没有红灯）=="
rm -rf "$RT/leds2"; mkdir -p "$RT/leds2/power:green"; mkled "$RT/leds2/power:green"
cat > "$RT/c5.conf" <<EOF
LEDS_ROOT=$RT/leds2
NET_ROOT=$RT/net
LOG_FILE=$RT/pled.log
TICK=0.1
CHECK_EVERY=5
EOF
P=$(run_bg "$RT/c5.conf"); sleep 1; s=""; for i in $(seq 1 12); do s="$s$(cat "$RT/leds2/power:green/brightness")"; sleep 0.05; done
kill $P 2>/dev/null; wait $P 2>/dev/null
echo "  绿灯采样序列: $s"
case "$s" in *1*0*|*0*1*) echo "  PASS  单色灯 → 无链路时绿灯闪烁（回退生效）";; *) echo "  FAIL  单色灯回退异常（序列 $s）";; esac

echo "== 场景6：子命令 status / once =="
echo 1 > "$RT/net/eth0/carrier"
echo "  status: $(POWER_LED_NET_CONF="$RT/c1.conf" bash "$SRC" status | tr '\n' ' ')"
echo "  once  : $(POWER_LED_NET_CONF="$RT/c1.conf" bash "$SRC" once)"
expect "once(有链路) → 绿灯亮" "1" "$(G)"
echo 0 > "$RT/net/eth0/carrier"
echo "  status: $(POWER_LED_NET_CONF="$RT/c1.conf" bash "$SRC" status | tr '\n' ' ')"

echo "== 场景7：手动覆盖 POWER_LED=off（有链路也必须全灭，权限最高）=="
echo 1 > "$RT/net/eth0/carrier"      # 链路是通的
cat > "$RT/override.conf" <<EOF
POWER_LED=off
EOF
cat > "$RT/c7.conf" <<EOF
LEDS_ROOT=$RT/leds
NET_ROOT=$RT/net
LOG_FILE=$RT/pled.log
TICK=0.1
CHECK_EVERY=5
LED_OVERRIDE_CONF=$RT/override.conf
EOF
P=$(run_bg "$RT/c7.conf"); sleep 1; SR=$(sample_red); kill $P 2>/dev/null; wait $P 2>/dev/null
echo "  红灯采样序列(全灭时应全0): $SR"
expect "覆盖off + 有链路 → 绿灯灭" "0" "$(G)"
expect "覆盖off + 有链路 → 红灯灭(不闪)" "0" "$(R)"
echo "  status: $(POWER_LED_NET_CONF="$RT/c7.conf" bash "$SRC" status | tr '\n' ' ')"

echo "== 场景8：覆盖改回 auto → 恢复自动（绿灯常亮）=="
cat > "$RT/override.conf" <<EOF
POWER_LED=auto
EOF
P=$(run_bg "$RT/c7.conf"); sleep 1; kill $P 2>/dev/null; wait $P 2>/dev/null
expect "覆盖auto + 有链路 → 绿灯常亮" "1" "$(G)"
expect "覆盖auto + 有链路 → 红灯灭" "0" "$(R)"

echo "== 日志 =="
sed 's/^/  /' "$RT/pled.log" 2>/dev/null
