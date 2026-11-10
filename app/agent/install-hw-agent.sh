#!/bin/bash
# 可选 root 代理安装/升级。只由管理员或已安装代理的升级回调调用。
set -eu
[ "$(id -u)" = 0 ] || { echo '需要以 root 安装硬件代理' >&2; exit 1; }
HERE="$(cd -- "$(dirname -- "$0")" && pwd -P)"
APP_NAME="${TRIM_APPNAME:-fnOS-S2}"
export S2_HWCTL_DIR="$(cd "$HERE/../server" && pwd -P)"
export S2_AGENT_KEY_FILE="${S2_AGENT_KEY_FILE:-${TRIM_PKGETC:-/var/apps/${APP_NAME}/etc}/agent.key}"
export S2_AGENT_GROUP="${S2_AGENT_GROUP:-$APP_NAME}"

# 先停旧的无认证进程。任何后续步骤失败都保持停止，不回退到无认证代理。
if systemctl cat s2hw-agent.service >/dev/null 2>&1; then
    systemctl stop s2hw-agent.service
    systemctl disable s2hw-agent.service >/dev/null 2>&1
fi
install -m 755 "$HERE/s2hw-agent.py" /usr/local/sbin/s2hw-agent.py
install -m 644 "$HERE/s2hw-agent.service" /etc/systemd/system/s2hw-agent.service
python3 /usr/local/sbin/s2hw-agent.py --init-key
install -d -m 755 /etc/systemd/system/s2hw-agent.service.d
python3 - <<'PY'
import json, os
with open('/etc/systemd/system/s2hw-agent.service.d/paths.conf', 'w') as f:
    f.write('[Service]\n')
    for name in ('S2_HWCTL_DIR', 'S2_AGENT_KEY_FILE'):
        # systemd Environment 支持双引号；%% 防止路径内的 % 被当作 specifier。
        value = (name + '=' + os.environ[name]).replace('%', '%%')
        f.write('Environment=' + json.dumps(value, ensure_ascii=False) + '\n')
PY
systemctl daemon-reload
systemctl enable --now s2hw-agent.service
