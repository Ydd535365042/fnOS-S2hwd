"""Native hardware service ownership only; no boot-file operations."""
import json
from pathlib import Path
import subprocess
import s2native
import s2security

HARDWARE = [name+".service" for name in ("fan-control", "fan-policy", "fan-api", "sata-led-manager",
    "sata-led-enable", "sata-led-watchdog", "power-led-net", "power-led-solid", "s2-power-led",
    "power-key-daemon", "random-mac", "random-mac-fixup", "set-hostname", "beep-boot", "led-set", "pwm-fancontrol")]

def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout + result.stderr


class Activation:
    allowed_units = frozenset(HARDWARE + ["fnos-s2-native.service", "s2-power-schedule.service"])
    def __init__(self, root="/", runner=command):
        self.root, self.runner = Path(root), runner
        self.state = self.root/"var/lib/fnos-s2"
        self.native = s2native.Native(root)

    def system(self, *args, required=True):
        code, output = self.runner(["systemctl", *args])
        if required and code: raise OSError("systemctl " + " ".join(args) + " 失败")
        return code, output

    def snapshot(self, units):
        result = {}
        for unit in units:
            _, output = self.system("show", unit, "--property=LoadState,UnitFileState,ActiveState", "--no-pager", required=False)
            fields = dict(line.split("=",1) for line in output.splitlines() if "=" in line)
            if fields.get("LoadState") == "loaded": result[unit] = fields
        return result

    def restore(self, states, start=True):
        self.validate_states(states)
        errors = []
        for unit, state in states.items():
            try:
                enabled = state.get("UnitFileState", "")
                if enabled == "enabled": self.system("enable", unit)
                elif enabled == "enabled-runtime": self.system("enable", "--runtime", unit)
                elif enabled == "disabled": self.system("disable", unit)
                if start and state.get("ActiveState") in ("active", "activating", "reloading"):
                    self.system("start", unit)
            except (OSError, subprocess.SubprocessError): errors.append(unit)
        if errors: raise OSError("服务恢复未完成："+", ".join(errors))

    def validate_states(self, states):
        if not isinstance(states, dict) or any(unit not in self.allowed_units or not isinstance(state, dict) for unit, state in states.items()):
            raise ValueError("服务恢复记录包含未知条目")

    def stop(self, states):
        for unit, state in states.items():
            if state.get("UnitFileState", "").startswith("masked"): continue
            if state.get("UnitFileState", "").startswith("enabled"): self.system("disable", unit)
            self.system("stop", unit)

    def remember(self, name, current):
        path = self.state/name
        if path.is_symlink(): raise ValueError("迁移记录不能是符号链接")
        if not path.exists(): s2security.atomic_text(str(path), json.dumps(current), mode=0o600)

    def activate(self):
        capabilities = self.native.capabilities()
        if not capabilities["ready"]: return {"ready": False, "detail": capabilities["detail"]}
        current = self.snapshot(HARDWARE)
        self.remember("hardware-services.json", current)
        fan = self.native.fan()
        self.remember("fan-original.json", {"pwm": s2native.number(s2native.read(fan/"pwm1")),
                                           "enable": s2native.number(s2native.read(fan/"pwm1_enable"))})
        config = self.root/"etc/s2hwctl/fan.conf"
        if not config.exists():
            legacy = self.root/"etc/fan-control.conf"
            text = s2native.read(legacy) or "TEMP_PROFILE=quiet\nCPU_T1=60\nCPU_T2=65\nCPU_T3=72\nCPU_T4=80\nCPU_D2=30\nCPU_D3=70\nHDD_T1=42\nHDD_T2=48\nHDD_T3=52\nHDD_T4=55\nHDD_D2=30\nHDD_D3=70\n"
            s2security.atomic_text(str(config), text+"\n", mode=0o600)
        self.native.fan_write(100)  # Verify cooling before stopping an existing owner.
        try:
            self.stop(current)
            s2security.atomic_text(str(self.root/"etc/s2hwctl/backend"), "native\n", mode=0o644)
        except (OSError, ValueError, subprocess.SubprocessError):
            self.restore(current)
            raise
        return {"ready": True, "detail": "标准硬件接口已就绪，旧控制服务状态已留存"}


if __name__ == "__main__":
    try:
        print(json.dumps(Activation().activate(), ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit("原生硬件迁移失败："+str(error))
