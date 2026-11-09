#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
power-key-daemon.py —— 电源键守护（长按 3 秒 → 蜂鸣一声 → 关机）

为什么要这个守护进程
--------------------
fnOS 的 ARM 镜像里 **没有 triggerhappy**（既无 triggerhappy.service 也无 /usr/sbin/thd），
所以 /etc/triggerhappy/triggers.d/power-key.conf 写好了也 **没有任何进程去执行**，
power-key.sh 从头到尾没被触发过。

而"短按一秒就关机"其实是 systemd-logind 干的：
logind.conf 里 HandlePowerKey 用默认值 poweroff，按下电源键立即关机。

因此本方案：
  1) logind 侧把电源键设为 ignore（见 power-key-logind.conf），不再抢着关机；
  2) 本守护进程直接读 /dev/input/event*，自己做长按判定：
       按下 < 3 秒就松开  → 忽略（只记日志）
       按住 >= 3 秒       → 蜂鸣一声 → poweroff

可配环境变量（一般不用改）：
  POWERKEY_LONG_SEC   长按阈值秒数，默认 3
  POWERKEY_BEEP       蜂鸣脚本，默认 /usr/local/sbin/beep-short.sh
  POWERKEY_SHUTDOWN   关机命令，默认 poweroff
"""

import errno
import glob
import os
import select
import struct
import subprocess
import sys
import threading
import time

EV_KEY = 0x01
KEY_POWER = 116

# struct input_event = timeval(两个 long) + type(u16) + code(u16) + value(i32)
# 原实现把 aarch64 的 24 字节写死；这里改为按当前平台 long 宽度推导，
# 32 位 ARM(long=4) 下自动得到 16 字节，换架构不会因事件错位而漏判长按。
_LONG_SIZE = struct.calcsize("l")
EVENT_FMT = "qqHHi" if _LONG_SIZE == 8 else "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

LONG_SEC = float(os.environ.get("POWERKEY_LONG_SEC", "3"))
BEEP = os.environ.get("POWERKEY_BEEP", "/usr/local/sbin/beep-short.sh")
SHUTDOWN = os.environ.get("POWERKEY_SHUTDOWN", "poweroff")


def log(msg):
    print("[power-key] %s" % msg, flush=True)


class PowerKeyWatcher(object):
    def __init__(self):
        self.fds = {}            # fd -> /dev/input/eventX
        self.paths = set()
        self.pressed = False
        self.timer = None
        self.lock = threading.Lock()
        self.shutting_down = False

    # ---------- 设备发现（支持热插拔 / 开机时设备还没出现） ----------
    def scan(self):
        for path in sorted(glob.glob("/dev/input/event*")):
            if path in self.paths:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self.fds[fd] = path
            self.paths.add(path)
            log("watch %s" % path)

    def drop(self, fd):
        p = self.fds.pop(fd, None)
        if p:
            self.paths.discard(p)
        try:
            os.close(fd)
        except OSError:
            pass

    # ---------- 长按判定 ----------
    def on_press(self):
        with self.lock:
            if self.pressed or self.shutting_down:
                return
            self.pressed = True
            self.timer = threading.Timer(LONG_SEC, self.on_long)
            self.timer.daemon = True
            self.timer.start()
        log("pressed — hold %.0fs to poweroff" % LONG_SEC)

    def on_release(self):
        with self.lock:
            self.pressed = False
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
        log("released before %.0fs — ignored" % LONG_SEC)

    def on_long(self):
        with self.lock:
            if not self.pressed or self.shutting_down:
                return
            self.shutting_down = True
        log("long press >= %.0fs — beep then poweroff" % LONG_SEC)

        # 1) 蜂鸣一声（复用已安装的 beep-short.sh：gpio_ctrl → GPIO18 → PWM 多路径兜底）
        try:
            subprocess.run([BEEP], timeout=8)
        except Exception as e:
            log("beep failed: %r" % (e,))

        # 2) 等这一声响完再关机
        time.sleep(0.4)

        # 3) 关机
        log("exec: %s" % SHUTDOWN)
        try:
            subprocess.run([SHUTDOWN], timeout=30)
        except Exception as e:
            log("poweroff failed: %r — sysrq fallback" % (e,))
            self.sysrq_fallback()

    @staticmethod
    def sysrq_fallback():
        try:
            with open("/proc/sys/kernel/sysrq", "w") as f:
                f.write("1")
            for k in ("s", "u", "b"):
                with open("/proc/sysrq-trigger", "w") as f:
                    f.write(k)
                time.sleep(1)
        except Exception as e:
            log("sysrq fallback failed: %r" % (e,))

    # ---------- 主循环 ----------
    def run(self):
        self.scan()
        last_scan = time.monotonic()

        while True:
            now = time.monotonic()
            if not self.fds or (now - last_scan) > 5:
                self.scan()
                last_scan = now

            if not self.fds:
                time.sleep(2)
                continue

            try:
                readable, _, _ = select.select(list(self.fds), [], [], 1.0)
            except (OSError, ValueError):
                time.sleep(1)
                continue

            for fd in readable:
                try:
                    data = os.read(fd, EVENT_SIZE * 8)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EINTR):
                        continue
                    self.drop(fd)          # 设备被拔出
                    continue

                for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    try:
                        _t1, _t2, etype, code, value = struct.unpack_from(
                            EVENT_FMT, data, off)
                    except struct.error:
                        break
                    if etype != EV_KEY or code != KEY_POWER:
                        continue
                    if value == 1:
                        self.on_press()
                    elif value == 0:
                        self.on_release()
                    # value == 2（按住期间的 repeat）忽略，定时器已在跑


def main():
    if os.geteuid() != 0:
        log("must run as root")
        return 1
    log("start: long=%.0fs beep=%s shutdown=%s" % (LONG_SEC, BEEP, SHUTDOWN))
    PowerKeyWatcher().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
