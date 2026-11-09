#!/usr/bin/env bash
# 离线自测：用 fake sysfs 验证 sata-led-manager.sh 的灯态
set -u
SRC="D:/S2-FNOS/zjtfnos/install-1014/install-1014/sata-led-manager.sh"
RT=/tmp/sataled_test
rm -rf "$RT"; mkdir -p "$RT/sys/class/gpio"
for g in 146 147 148 149 86; do
  mkdir -p "$RT/sys/class/gpio/gpio$g"
  echo 0 > "$RT/sys/class/gpio/gpio$g/value"
  echo in  > "$RT/sys/class/gpio/gpio$g/direction"
done
mkdir -p "$RT/slot1/block" "$RT/slot2/block" "$RT/sys/block"

BASE_CONF="$RT/base.conf"
cat > "$BASE_CONF" <<EOF
SYSFS_ROOT=$RT/sys
SLOT1_PATH=$RT/slot1/block
SLOT2_PATH=$RT/slot2/block
LOG_FILE=$RT/led.log
TICK=0.2
EOF

vals() {
  echo "  R1(g146)=$(cat $RT/sys/class/gpio/gpio146/value) G1(g147)=$(cat $RT/sys/class/gpio/gpio147/value) R2(g148)=$(cat $RT/sys/class/gpio/gpio148/value) G2(g149)=$(cat $RT/sys/class/gpio/gpio149/value) SW(g86)=$(cat $RT/sys/class/gpio/gpio86/value)"
}
expect() {
  local desc="$1" want="$2" got="$3"
  if [ "$want" = "$got" ]; then echo "  PASS  $desc"; else echo "  FAIL  $desc  期望[$want] 实际[$got]"; fi
}
state() {
  echo "$(cat $RT/sys/class/gpio/gpio146/value)$(cat $RT/sys/class/gpio/gpio147/value)$(cat $RT/sys/class/gpio/gpio148/value)$(cat $RT/sys/class/gpio/gpio149/value)"
}

run_bg() {  # $1=conf文件
  SATA_LED_CONF="$1" bash "$SRC" >/dev/null 2>&1 &
  echo $!
}

echo "== 场景1：两个槽位都没有硬盘 =="
cat "$BASE_CONF" > "$RT/c1.conf"
P=$(run_bg "$RT/c1.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "无盘 → 红绿全灭" "0000" "$(state)"

echo "== 场景2：槽位1 插入硬盘（默认 PRESENT_GREEN=1 → 绿灯常亮）=="
mkdir -p "$RT/slot1/block/sda" "$RT/sys/block/sda"; echo "0 0 0 0 0 0 0 0 0 0 0" > "$RT/sys/block/sda/stat"
cat "$BASE_CONF" > "$RT/c2.conf"
P=$(run_bg "$RT/c2.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "有盘 → 槽1绿灯常亮" "0100" "$(state)"

echo "== 场景3：槽位2 也插盘 =="
mkdir -p "$RT/slot2/block/sdb" "$RT/sys/block/sdb"; echo "0 0 0 0 0 0 0 0 0 0 0" > "$RT/sys/block/sdb/stat"
cat "$BASE_CONF" > "$RT/c3.conf"
P=$(run_bg "$RT/c3.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "两盘都在 → 两槽绿灯常亮" "0101" "$(state)"

echo "== 场景4：拔掉两块盘 =="
rm -rf "$RT/slot1/block/sda" "$RT/slot2/block/sdb" "$RT/sys/block/sda" "$RT/sys/block/sdb"
cat "$BASE_CONF" > "$RT/c4.conf"
P=$(run_bg "$RT/c4.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "拔盘 → 全灭" "0000" "$(state)"

echo "== 场景5：KEEP_OFF_WHEN_PRESENT=0（有盘交回系统，不干预）=="
mkdir -p "$RT/slot1/block/sda" "$RT/sys/block/sda"; echo "0 0 0 0 0 0 0 0 0 0 0" > "$RT/sys/block/sda/stat"
echo 0 > "$RT/sys/class/gpio/gpio146/value"; echo 1 > "$RT/sys/class/gpio/gpio147/value"
cp "$BASE_CONF" "$RT/c5.conf"; echo "KEEP_OFF_WHEN_PRESENT=0" >> "$RT/c5.conf"
P=$(run_bg "$RT/c5.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "有盘且 release → GPIO 保持系统写入值(0,1,0,0)" "0100" "$(state)"

echo "== 场景6：可配置回归 —— PRESENT_GREEN=1（保留日后加灯的能力）=="
echo 0 > "$RT/sys/class/gpio/gpio147/value"
cp "$BASE_CONF" "$RT/c6.conf"; echo "PRESENT_GREEN=1" >> "$RT/c6.conf"
P=$(run_bg "$RT/c6.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "有盘 → 槽1绿灯亮" "0100" "$(state)"

echo "== 场景7：手动覆盖 SATA_LED=off（有盘也全灭，权限最高）=="
cat > "$RT/override.conf" <<EOF
SATA_LED=off
EOF
cp "$BASE_CONF" "$RT/c7.conf"; echo "LED_OVERRIDE_CONF=$RT/override.conf" >> "$RT/c7.conf"
P=$(run_bg "$RT/c7.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "覆盖off + 有盘 → 红绿全灭" "0000" "$(state)"

echo "== 场景8：覆盖 off 且 KEEP_OFF_WHEN_PRESENT=0（off 必须压过 release 分支）=="
echo 1 > "$RT/sys/class/gpio/gpio147/value"
cp "$BASE_CONF" "$RT/c8.conf"; echo "KEEP_OFF_WHEN_PRESENT=0" >> "$RT/c8.conf"; echo "LED_OVERRIDE_CONF=$RT/override.conf" >> "$RT/c8.conf"
P=$(run_bg "$RT/c8.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "覆盖off 压过 release → 全灭" "0000" "$(state)"

echo "== 场景9：覆盖改回 auto → 有盘恢复绿灯常亮 =="
cat > "$RT/override.conf" <<EOF
SATA_LED=auto
EOF
P=$(run_bg "$RT/c7.conf"); sleep 1.5; kill $P 2>/dev/null; wait $P 2>/dev/null
vals; expect "覆盖auto + 有盘 → 槽1绿灯亮" "0100" "$(state)"

echo "== 日志 =="
cat "$RT/led.log" 2>/dev/null | sed 's/^/  /'
