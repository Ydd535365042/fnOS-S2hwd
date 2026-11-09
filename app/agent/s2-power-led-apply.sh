#!/bin/bash
# 开机套用页面保存的电源灯配置（自包含，卸载应用后也不会残留坏依赖）
CONF=/etc/s2hwctl/power-led.conf
[ -f "$CONF" ] || exit 0
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || exit 0

"$PY" - "$CONF" <<'PYEOF'
import json, os, sys

def w(p, v):
    try:
        with open(p, 'w') as f:
            f.write(str(v))
        return True
    except Exception:
        return False

try:
    conf = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)

for name, cfg in (conf.get('leds') or {}).items():
    base = '/sys/class/leds/' + name
    if not os.path.isdir(base):
        continue
    mode = cfg.get('mode', 'on')
    b = cfg.get('brightness')
    try:
        maxb = int(open(base + '/max_brightness').read().strip() or 255)
    except Exception:
        maxb = 255
    if mode == 'blink':
        w(base + '/trigger', 'timer')
        w(base + '/delay_on', cfg.get('on_ms', 500))
        w(base + '/delay_off', cfg.get('off_ms', 500))
    else:
        w(base + '/trigger', 'none')
        v = maxb if mode == 'on' else 0
        if b is not None and mode == 'on':
            v = max(0, min(maxb, int(b)))
        w(base + '/brightness', v)
PYEOF
exit 0
