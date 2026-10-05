---
ac: AC-ISO-F14-1
kind: sandbox
model: n/a
fixtures:
  - fixtures/iso-f14-worker
timeout_s: 300
---

# AC-ISO-F14-1 — a Hermes-driven sandbox reaches egress ONLY through the OneCLI proxy (curl = 200)

From inside a sandbox that Hermes spawns off the rendered `iso-f14-worker` profile —
through CRED-F28's podman wrapper (`HERMES_DOCKER_BINARY`, fixed infra) — an outbound
HTTPS request through the rendered `https_proxy` returns HTTP **200** via the OneCLI
proxy, proving the render's egress env + CA route egress through OneCLI. The three
`sandbox:` criteria (AC-ISO-F14-1/2/5) share ONE spawn: this scenario spawns the
container; AC-ISO-F14-2 and AC-ISO-F14-5 reuse it (do NOT spawn a second).

## Setup
- The `iso-f14-worker` fixture profile is installed (the tester installs the
  `fixtures:` list before this Setup runs — do NOT re-install it here) and its OneCLI
  agent has its secrets assigned (CRED-F28 pre-flight).
- `terminal.docker_image` in the fixture is substituted for the actual fleet sandbox
  image (one shipping `bash` + `curl`; `docker.py` execs `bash`).
- The §4c rootless-podman socket is reachable (`CONTAINER_HOST`, `$PODMAN` v3.4.4). The
  S6 pre-flight passes the bare cgroup-probe argv and asserts
  `inspect --format '{{.HostConfig.Memory}}'` != 0 on a Hermes-spawned sandbox.
- `HERMES_DOCKER_BINARY` = CRED-F28's podman wrapper. A sandbox is spawnable off the
  rendered profile through the wrapper via the item-5 driver
  (`python -c 'from tools.terminal_tool import terminal_tool; ...'` under the fixture
  `HERMES_HOME`). This exercises CRED-F28's wrapper as FIXED infra; it does not build
  or modify it.
- The host CA copy at the fixture's `ca_host_path` (`/opt/onecli/ca.crt`, readable by
  uid 1001) is present — the item-9 operator deployment step / CRED-F28 onboard writes
  the FILE; ISO-F14 renders only the mount entry.

## Steps
1. Drive one `_DockerEnvironment` spawn off the installed rendered profile through the
   wrapper (no model call), then run a trivial `true` via `execute`.
   → expect: the container starts and `true` exits 0.
2. From inside the sandbox, using the rendered `https_proxy`, run
   `curl -s -o /dev/null -w '%{http_code}' https://<a provider host reachable via OneCLI>`.
   **No `-k`** — the curl must rely on the mounted OneCLI CA (via the rendered
   `CURL_CA_BUNDLE`/`SSL_CERT_FILE`), so a 200 proves the CA route, not a TLS bypass.
   → expect: `200`.
3. Read `/proc/1/uid_map` from inside the sandbox.
   → expect: it contains the row `0 1001 1` (a valid rootless map may carry additional
   subordinate rows).

## Pass
The curl returns `200` through the OneCLI proxy (TLS verified against the mounted CA)
AND `/proc/1/uid_map` contains the `0 1001 1` rootless row.

## Evidence
- `scenario-AC-ISO-F14-1/podman-calls.log` — the exact `$PODMAN run` line + exit code.
- `scenario-AC-ISO-F14-1/curl-200.log` — the `%{http_code}` (200).
- `scenario-AC-ISO-F14-1/uid_map.log` — the `/proc/1/uid_map` contents.
