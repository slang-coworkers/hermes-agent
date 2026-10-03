"""Record the network namespace of each fleet process in this sandbox (from /proc; works without procps).

Prints "self <netns>" for this process, then "<kind> <pid> <netns>" for every hermes serve, gateway,
dashboard and tui_gateway (PTY child) process, then "netns_distinct <n>" over all of those lines.
A process whose namespace link cannot be read prints "unreadable". Names and inode ids only.
"""
import os

from gateway_count import hermes_subcommand


def kind(argv):
    rest = hermes_subcommand(argv)
    if rest is not None:
        if rest[:2] in (["gateway", "run"], ["gateway", "restart"]):
            return "gateway"
        if rest[:1] in (["serve"], ["dashboard"]):
            return rest[0]
        return None
    if len(argv) >= 3 and os.path.basename(argv[0]).startswith("python") and argv[1:3] == ["-m", "tui_gateway.entry"]:
        return "pty_child"
    return None


def netns(pid):
    try:
        return os.readlink("/proc/%s/ns/net" % pid)
    except OSError:
        return "unreadable"


def main():
    seen = {netns("self")}
    print("self", netns("self"))
    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
        try:
            with open("/proc/" + pid + "/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        k = kind(argv)
        if k:
            ns = netns(pid)
            seen.add(ns)
            print(k, pid, ns)
    print("netns_distinct", len(seen))


if __name__ == "__main__":
    main()
