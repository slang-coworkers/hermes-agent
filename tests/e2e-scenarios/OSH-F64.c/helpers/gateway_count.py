"""Count `hermes gateway run` processes from /proc (works without procps). Prints "gateway_processes <n>"."""
import os

n = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open("/proc/" + pid + "/cmdline", "rb") as fh:
            argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        continue
    if any(a.endswith("hermes") for a in argv) and "gateway" in argv and "run" in argv:
        n += 1
print("gateway_processes", n)
