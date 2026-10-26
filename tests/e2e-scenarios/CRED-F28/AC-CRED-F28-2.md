---
ac: AC-CRED-F28-2
kind: sandbox
fixtures: [cred-f28-bot-a, cred-f28-bot-b]
timeout_s: 600
---

# AC-CRED-F28-2 — per-profile OneCLI identity + request-time injection (sandbox)

From a profile SUBPROCESS (a Hermes-driven `_DockerEnvironment` spawn, not a
gateway turn or Bot Chat), `curl` through the rendered `https_proxy` returns
**401 for profile A before its secrets are assigned, 200 after
`onecli agents set-secrets`, and 401 for a sibling profile B with no grant** —
proving per-profile identity plus request-time credential injection, with no
credential value ever inside the sandbox.

## Fixtures

- `fixtures/cred-f28-bot-a/` and `fixtures/cred-f28-bot-b/` — two profile dirs,
  each with `distribution.yaml` at its root and a distinct basename.
- Each renders `terminal.docker_image: hermes-sandbox:pinned` (ships bash+curl),
  `terminal.container_memory: 1024`, `terminal.container_cpu: 0.5`,
  `terminal.docker_persist_across_processes: false`, and
  `plugins.entries.podman-onecli.settings.profile_secret_sets =
  {cred-f28-bot-a: [], cred-f28-bot-b: []}` (both START ungranted).
- The per-profile `terminal.docker_env` (the proxy URL carrying this profile's
  `aoc_` token, from `GET /v1/container-config`), `terminal.docker_volumes`
  (the read-only CA mount) and `HERMES_DOCKER_BINARY` are materialized by the
  render/substrate (ISO-F14, not this row's code); the driver below supplies
  `HERMES_DOCKER_BINARY` and the wrapper's `PODMAN_ONECLI_*` coordinates.

## Setup (after the fixtures are installed)

Onboarding has run for both profiles:

```bash
HERMES_HOME=$TB/home/profiles/cred-f28-bot-a hermes onecli-onboard --profile cred-f28-bot-a
HERMES_HOME=$TB/home/profiles/cred-f28-bot-b hermes onecli-onboard --profile cred-f28-bot-b
```

Each ensured its OneCLI agent and called `set_secrets(..., [])` (empty grant
from `profile_secret_sets`), so both agents exist with NO secrets.

Fixed values:

- `PODMAN=/workspace/extra/podman-bin/podman-remote-static`
- `CONTAINER_HOST=unix:///workspace/extra/podman/podman.sock`
- wrapper `$WT/plugins/podman-onecli/bin/podman-onecli-wrap`
- expected proxy `172.17.0.1:10255`
- host CA copy readable by uid 1001 at `/opt/onecli/ca.crt`
- container CA `/etc/ssl/certs/hermes-egress-ca.crt`
- egress target `https://inference-api.nvidia.com/v1/models`

Every curl uses `--cacert /etc/ssl/certs/hermes-egress-ca.crt` (never `-k`).

## Driver

`drive <profile> <phase>` — one Hermes-driven spawn, no model call. Artifacts
are `<phase>`-suffixed so the three runs never overwrite each other; everything
is written before the interpreter exits, since `persist=false` removes the
container at atexit.

```bash
drive() {  # $1 = profile, $2 = phase (pre|post|ungranted)
  local d="$ART/scenario-AC-CRED-F28-2"; mkdir -p "$d"
  HERMES_HOME="$TB/home/profiles/$1" \
  HERMES_DOCKER_BINARY="$WT/plugins/podman-onecli/bin/podman-onecli-wrap" \
  PODMAN_ONECLI_PODMAN="$PODMAN" PODMAN_ONECLI_EXPECTED_PROXY=172.17.0.1:10255 \
  PODMAN_ONECLI_EXPECTED_CA=/etc/ssl/certs/hermes-egress-ca.crt \
  PODMAN_ONECLI_LOG="$d/podman-calls.log" CONTAINER_HOST="$CONTAINER_HOST" \
  `# Both fixtures are [] (ungranted), so the render forwards only proxy/CA/NO_PROXY` \
  `# vars — all in the wrapper's default allowlist. A profile GRANTED a provider` \
  `# must also export PODMAN_ONECLI_ALLOWED_ENV=<placeholder names> or the wrapper` \
  `# fails closed on the un-declared -e (e.g. PODMAN_ONECLI_ALLOWED_ENV=ANTHROPIC_API_KEY).` \
  ART="$d" BOT="$1" PHASE="$2" PODMAN="$PODMAN" python3 - <<'PY'
import json, os, subprocess
from tools.terminal_tool import terminal_tool
d, bot, phase, podman = os.environ["ART"], os.environ["BOT"], os.environ["PHASE"], os.environ["PODMAN"]
tag = f"{bot}-{phase}"
code = json.loads(terminal_tool("curl -sS --cacert /etc/ssl/certs/hermes-egress-ca.crt -o /dev/null -w %{http_code} https://inference-api.nvidia.com/v1/models"))["output"].strip()
open(f"{d}/http-{tag}.txt", "w", encoding="utf-8").write(code)
open(f"{d}/uid_map-{tag}.txt", "w", encoding="utf-8").write(json.loads(terminal_tool("cat /proc/1/uid_map"))["output"])
ps = subprocess.run([podman, "ps", "-a", "--filter", "label=hermes-agent=1",
                     "--filter", f"label=hermes-profile={bot}", "--format", "{{.ID}} {{.Names}}"],
                    capture_output=True, text=True).stdout
open(f"{d}/ps-{tag}.txt", "w", encoding="utf-8").write(ps)
cid = ps.split()[0] if ps.strip() else ""
if cid:
    insp = subprocess.run([podman, "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}", cid],
                          capture_output=True, text=True).stdout
    open(f"{d}/inspect-{tag}.txt", "w", encoding="utf-8").write(insp)
PY
}
```

`terminal_tool` execs bash+curl inside the sandbox (`docker.py:1670-1675`); its
JSON return carries `output`/`exit_code` (`tools/terminal_tool.py:3763`). The
wrapper's forwarded launch argv lands in `podman-calls.log`; `ps`/`inspect` are
queried against the live container via `$PODMAN` + `CONTAINER_HOST` INSIDE the
same process, before atexit stop+rm removes it.

## Steps

1. bot-a, ungranted: `drive cred-f28-bot-a pre` → expect
   `http-cred-f28-bot-a-pre.txt == 401` (identity exists, no secret assigned).
2. `HERMES_HOME=$TB/home/profiles/cred-f28-bot-a hermes onecli-onboard --profile cred-f28-bot-a`
   with `profile_secret_sets[cred-f28-bot-a]` now naming bot-a's provider set
   (selective — never mode all) → `set_secrets` grants A;
   `drive cred-f28-bot-a post` → expect `http-cred-f28-bot-a-post.txt == 200`
   (OneCLI injects A's credential at request time).
3. From the bot-a artifacts: `uid_map-cred-f28-bot-a-post.txt` contains the row
   `0 1001 1`; `inspect-cred-f28-bot-a-post.txt` shows `HostConfig.Memory != 0`;
   and the launch argv (`--init`, `_BASE_SECURITY_ARGS`, name-only `-e`,
   `--cpus 0.5 --memory 1024m --pids-limit 256`, `--label nv.hermes-ppid=…`)
   appears in `podman-calls.log`.
4. Sibling bot-b, still ungranted: `drive cred-f28-bot-b ungranted` → expect
   `http-cred-f28-bot-b-ungranted.txt == 401`.

## Pass

The following exits 0 —

```bash
d="$ART/scenario-AC-CRED-F28-2"
test "$(cat "$d/http-cred-f28-bot-a-pre.txt")" = 401 &&
test "$(cat "$d/http-cred-f28-bot-a-post.txt")" = 200 &&
test "$(cat "$d/http-cred-f28-bot-b-ungranted.txt")" = 401
```

— plus `uid_map-cred-f28-bot-a-post.txt` shows `0 1001 1`,
`inspect-cred-f28-bot-a-post.txt` shows non-zero memory, and
`! rg -n 'aoc_' "$d"` (no token leaked to any artifact).

## Evidence

`scenario-AC-CRED-F28-2/` — `podman-calls.log` (forwarded launch argv per
profile), `http-cred-f28-bot-a-pre.txt` (`401`),
`http-cred-f28-bot-a-post.txt` (`200`), `http-cred-f28-bot-b-ungranted.txt`
(`401`), and the matching `ps-<tag>.txt`, `inspect-<tag>.txt`
(`HostConfig.Memory`/`NanoCpus`), `uid_map-<tag>.txt` (`0 1001 1`) per
`<tag>=<bot>-<phase>`. Record only env-var NAMES, never values; the run refuses
to write any `aoc_` token to an artifact (the `rg` check is part of Pass).
