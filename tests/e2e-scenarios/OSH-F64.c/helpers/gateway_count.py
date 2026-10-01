"""Count `hermes gateway run` and `hermes serve` processes from /proc (works without procps).

Prints "gateway_processes <n>" (the gated count) and "serve_processes <n>" (recorded only).
"""
import os

n = 0
serve = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open("/proc/" + pid + "/cmdline", "rb") as fh:
            argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        continue
    if not any(a.endswith("hermes") for a in argv):
        continue
    if "gateway" in argv and "run" in argv:
        n += 1
    elif "serve" in argv:
        serve += 1
print("gateway_processes", n)
print("serve_processes", serve)
