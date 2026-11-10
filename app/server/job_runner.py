#!/usr/bin/env python3
"""Detached, bounded script supervisor. Persists result even if the web server restarts."""
import os
import signal
import subprocess
import sys
import time


def supervise(bash, script, log, result, timeout, cwd, log_limit=16 * 1024 * 1024):
    cancelled = [False]
    signal.signal(signal.SIGTERM, lambda *_: cancelled.__setitem__(0, True))
    code = 1
    try:
        with open(log, "ab", buffering=0) as output:
            process = subprocess.Popen([bash, script], cwd=cwd, stdout=output, stderr=output,
                                       stdin=subprocess.DEVNULL, start_new_session=os.name == "posix")
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                log_full = os.path.getsize(log) >= log_limit
                if cancelled[0] or time.monotonic() >= deadline or log_full:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        process.wait()
                    code = 130 if cancelled[0] else 125 if log_full else 124
                    output.write(("\n任务已取消\n" if cancelled[0] else "\n任务日志达到上限，已终止\n" if log_full else "\n任务超时，已终止\n").encode())
                    break
                time.sleep(0.1)
            else:
                code = process.returncode
    except OSError as error:
        with open(log, "ab") as output:
            output.write(("无法执行任务：" + type(error).__name__ + "\n").encode())
    finally:
        with open(result, "w") as stream:
            stream.write(str(max(0, code) if code >= 0 else 128 - code) + "\n")
        if os.path.basename(script).startswith(".s2job-"):
            try:
                os.unlink(script)
            except FileNotFoundError:
                pass
    return code


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6]))
