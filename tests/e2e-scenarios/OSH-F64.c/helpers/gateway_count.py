"""Count the Hermes gateway and `hermes serve` processes from /proc (works without procps).

Prints "gateway_processes <n>" (the gated count) and "serve_processes <n>" (recorded only).
"""
import os

HERMES_SCRIPTS = ("hermes", "hermes.real")


def hermes_subcommand(argv):
    """The argv after the hermes program token, or None when the process is not hermes itself.

    The program must be argv[0], or the script/`-m hermes_cli.main` right after a python
    interpreter. That keeps wrappers that merely carry a hermes command line further along
    (the restart watcher's `python -c`, `-m hermes_cli.stderr_timestamp ... -- <cmd>`) from
    being counted alongside the gateway they launch.
    """
    if not argv:
        return None
    if os.path.basename(argv[0]) in HERMES_SCRIPTS:
        rest = argv[1:]
    elif os.path.basename(argv[0]).startswith("python"):
        i = 1
        while i < len(argv) and argv[i].startswith("-") and argv[i] not in ("-m", "-c"):
            i += 1
        if i < len(argv) and argv[i] == "-m":
            if argv[i + 1:i + 2] != ["hermes_cli.main"]:
                return None
            rest = argv[i + 2:]
        elif i < len(argv) and os.path.basename(argv[i]) in HERMES_SCRIPTS:
            rest = argv[i + 1:]
        else:
            return None
    else:
        return None
    while rest and rest[0] in ("-p", "--profile"):
        rest = rest[2:]
    while rest and rest[0].startswith("--profile="):
        rest = rest[1:]
    return rest


def classify(argv):
    rest = hermes_subcommand(argv)
    if rest is None:
        return None
    # With no service manager, `hermes gateway restart` runs run_gateway() in its own process, so
    # the live gateway keeps the `gateway restart` argv (release gateway/status.py:547-553).
    if rest[:2] in (["gateway", "run"], ["gateway", "restart"]):
        return "gateway"
    if rest[:1] == ["serve"]:
        return "serve"
    return None


def main():
    counts = {"gateway": 0, "serve": 0}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/" + pid + "/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        kind = classify(argv)
        if kind:
            counts[kind] += 1
    print("gateway_processes", counts["gateway"])
    print("serve_processes", counts["serve"])


if __name__ == "__main__":
    main()
