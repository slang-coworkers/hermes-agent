---
ac: AC-OSH-F64-4
kind: live
model: live
base_url: https://inference.local/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
api_key: sk-OPENSHELL-PROXY-REWRITE
fixtures:
  - fixtures/osh-f64c-gateway
  - fixtures/osh-f64c-worker-orchestrator
  - fixtures/osh-f64c-worker-builder
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
LIVE_ROUND_CALLS_MAX: 120
LIVE_ROUND_BUDGET_USD: 15
timeout_s: 600
---

# AC-OSH-F64-4 (evidence clause only) — the room screenshots, captured from the canonical hidden Bot Chats through the nv-bot-chat tab (live)

Carried from OSH-F64 by operator ruling 3758, **evidence clause only**. OSH-F64 could not capture the
room screenshots `scenario-AC-OSH-F64-4/step-1..5.png`, for the same reason as AC-5: the stock web
dashboard cannot open a hidden Bot Chat. This scenario re-runs the parent's two-bot exchange on an
`osh-f64c-` fleet, then captures each event from the canonical hidden Bot Chats through the nv-bot-chat
tab, on ONE forwarded dashboard URL. Steps 1–3 land in the orchestrator's Bot Chat, steps 4–5 in the
builder's. AC-4's behaviour clauses (delivery, wiring gate, sandbox routing/isolation, veto, one gateway)
stay PASS on OSH-F64 and are **not re-graded here**.

**Inference route (cycle 2).** The front-matter provider block is OSH-F64.b's managed route
(`tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-6.md:1-16`): `https://inference.local/v1`, `chat_completions`,
`aws/anthropic/bedrock-claude-opus-5-5`, and the fixed rewrite-trigger placeholder as `api_key`. There is no
`key_env`, no OneCLI proxy and no credential anywhere in the sandbox: OpenShell rewrites the real credential in at
the boundary, and OpenShell + APF is the single authority. A model call leaves the sandbox only as
`POST https://inference.local/v1/chat/completions`. The caps are unchanged: 40 calls / $5 per scenario,
120 calls / $15 per round.

**Gated spec.** At this base the single-authority render is opt-in: it runs only when the spec carries
`egress.inference_provider` (`plugins/nv-coworker-compose/compose.py:1261-1273`). The parent spec has none, so this
scenario ships its own, `spec/coworker-types.yaml` + `spec/spines/base.yaml`. That is the parent spec with exactly
three deltas (`project: osh-f64c`, `inference_route: "inference.local:443"`, OSH-F64.b's `inference_provider` block
with `attribution_tag_prefix: osh-f64c`) on OSH-F64.b's spine. The parent's roles, identities,
`skills.external_dirs`, rooms and `wires` ring are kept, so orchestrator↔builder starts unwired. The three fixtures
are in the OSH-F64.b shape: `compatible-endpoint` holding the placeholder, `secrets.onecli.enabled: false`, and
`X-Hermes-Profile` / `attribution_tag` = `osh-f64c-<role>` (`osh-f64c-default` on the gateway seed, matching the
default render, because the installer never rewrites the default provider block).

**Lane.** Serialized behind the OSH-F64 lane: never run while the OSH-F64 tester holds it. Every sandbox and
policy name uses the `osh-f64c-` prefix (gateway `osh-f64c-gw`, workers `osh-f64c-orchestrator` /
`osh-f64c-builder`). Host-side writes go only under `/workspace/extra/hermes-fleet-testbed/osh-f64c`;
in-sandbox renders go under `$HERMES_HOME/.osh-f64c/render`. Nothing is written under the OSH-F64 lane's
`/workspace/extra/hermes-fleet-testbed/osh-f64`. If the run uses the `brev-hermes` fleet host instead of a
throwaway `osh-f64c-gw`, the same lane-local roots apply.

## Setup

The `fixtures:` list is installed before this section (`osh-f64c-gateway` is the gateway/default seed,
the two workers the served coworkers); nothing here re-installs one. The gateway seed keeps
`nv-bot-chat` in `plugins.enabled`, which the installer UNIONs with the spine set.

**Lane preconditions, verified and never repaired here** (`website/docs/user-guide/fleet-openshell.md:511-535`;
`tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-6.md:37-56`). They are operator-owned: PROVIDER READY (the OpenShell
inference provider behind `inference.local` credential-rewrites a placeholder request and forwards it); the run env
carries `HTTPS_PROXY=10.200.0.1:3128`, OpenShell's launch env (`fleet-openshell.md:124-127`); the gateway image is
`osh-f64-gateway:pinned`, rebuilt by the lane autobuilder at the PR head under test (LANE READY names that sha),
never the `:f64b-a2d9f261` tag. The OpenShell Sandbox CA is a lane-managed property, checked by P1/P3, and a miss
is `FAIL(env)` before any counted model call: never a scenario-side install, CA copy or env override. The fleet
start path is the operator-approved background start below (operator msg 508), not an in-sandbox watchdog. No live turn forces `tool_choice`.

```bash
set -o pipefail                                            # a gate piped into tee keeps its own exit code
SCN=$WT/tests/e2e-scenarios/OSH-F64.c; AC=AC-OSH-F64-4; mkdir -p $ART/scenario-$AC
ROOT=/workspace/extra/hermes-fleet-testbed/osh-f64c        # lane-local host policy/render root
SB=osh-f64c-gw; P=<free port in 29000-29999>
ROUTE=https://inference.local/v1                            # the only billing_base_url a model call may carry
T0=$(date +%s)                                              # scenario start; the route record covers sessions started since
SPEC='"$HERMES_HOME/.osh-f64c/spec/coworker-types.yaml"'   # in-sandbox path; staged in step 2, expands inside the sandbox
# Each sx call is a fresh login shell, so every variable a later in-sandbox command reads is exported here,
# not in the step that first sets it. HERMES_HOME: the image leaves it unset and Hermes uses $HOME/.hermes
# (= /sandbox/.hermes, hermes_constants.py:59, :71-74). HERMES_MANAGED_DIR: read by phase-a, compose, serve,
# install and onboard; Hermes resolves it only from the env var or /etc/hermes (managed_scope.py:65-71), and the
# image has no /etc/hermes. PY: the Hermes venv's python.
HX='export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"; export HERMES_MANAGED_DIR="$HERMES_HOME/managed"; PY=/opt/hermes/.venv/bin/python; [ -x "$PY" ] || PY=$(sed -n "1s/^#!//p" "$(command -v hermes)" 2>/dev/null);'
sx() {  # sx [-t <secs>] '<command>': run it in $SB after $HX; -t bounds it (timeout 0 = no bound)
  local t=0; [ "$1" = -t ] && { t=$2; shift 2; }
  timeout "$t" openshell sandbox exec "$SB" -- sh -lc "$HX $1"
}
stage() {  # as in AC-OSH-F64-5: copy a small local dir into the sandbox through exec argv
  b64=$(tar -C "$(dirname "$1")" --exclude=__pycache__ -czf - "$(basename "$1")" | base64 -w0)
  sx "mkdir -p $2 && echo $b64 | base64 -d | tar -C $2 -xzf -"
}
# G3 guard env (a test-harness variable, not Hermes config): exported in the SAME one-line command
# as every fleet start, so every interpreter it spawns imports the staged route guard.
GENV='export PY PYTHONPATH="$HERMES_HOME/.osh-f64c/helpers/route_guard" OSH_F64C_ROUTE_GUARD_DIR="$HERMES_HOME/.osh-f64c/guard";'
bgrun() {  # bgrun <name> '<cmd>': a one-shot on the Setup-5 start path, polled in <=240 s slices (broker cuts ~300 s)
  # <cmd> carries no single quote; write in-sandbox values as "$PY" / "$HERMES_HOME" (exported by HX + GENV).
  sx "rm -f /tmp/osh-f64c-$1.rc; $GENV setsid nohup sh -c '$2; echo \$? > /tmp/osh-f64c-$1.rc' > /tmp/osh-f64c-$1.out 2>&1 < /dev/null &"
  for s in 1 2 3; do sx -t 240 "i=0; until [ -s /tmp/osh-f64c-$1.rc ] || [ \$i -ge 110 ]; do sleep 2; i=\$((i+1)); done"; done
  sx -t 60 "cat /tmp/osh-f64c-$1.out; rc=\$(cat /tmp/osh-f64c-$1.rc 2>/dev/null || echo none); echo rc=\$rc; [ \"\$rc\" = 0 ]"
}
```

**Fleet-process start path — operator ruling D1 = option 2 (operator msg 508, 2026-10-03T05:10Z):** "I grant
the exception. Start serve, the gateway and the dashboard with `openshell sandbox exec` + `nohup ... &`, exactly as
OSH-F64.b Part B did ... Keep the fail-closed route gates the builder built (G2/G3/G4)." The "in-sandbox watchdog"
of `website/docs/user-guide/fleet-openshell.md:524-526` never existed in the gateway image; this scenario does not
edit that doc. So `hermes serve`, the install (with its foreground `gateway restart`), `hermes dashboard`, the
step-5 builder CLI and the P3/P4/G3 probes each start with their own `sx "$GENV … setsid nohup <cmd> > <log> 2>&1
< /dev/null &"` (`bgrun` for the one-shots). Netns is recorded, not assumed: every guard record's netns must equal
the dashboard's (G2/G4), and P3 must pass in it.

**Every command run inside `osh-f64c-gw` goes through `sx`.** That includes the parent's in-sandbox
steps (1, 2a, 3), the install and the onboard. `install-into-sandbox.sh` itself refuses an unset
`HERMES_HOME` (`plugins/nv-coworker-compose/openshell/install-into-sandbox.sh:66-71`). Host-side
commands (`mkdir`, `cp`, `curl`, `socat`, the host redirections, `openshell sandbox create|delete`,
`openshell forward`) stay outside `sx`.

1. **Host-side gateway-policy pre-render.** The gated `policy-gateway.yaml` is a compose output
   (`plugins/nv-coworker-compose/compose.py:4176-4204`), and it binds only at create time: `sandbox create --policy`
   binds and enforces it inline, and the plan has no policy-update step (`compose.py:1888-1893`;
   `plugins/nv-coworker-compose/openshell/installer.py:515-519`). So render it BEFORE the gateway sandbox exists,
   tester-side, with the PR worktree's own venv and plugin, under a throwaway `HERMES_HOME` (compose is a pure render,
   `plugins/nv-coworker-compose/__init__.py:927-938`):
   ```bash
   CH=$ART/scenario-$AC/compose-home; mkdir -p $CH $ROOT
   printf 'plugins:\n  enabled:\n    - nv-coworker-compose\n' > $CH/config.yaml
   HERMES_HOME=$CH HERMES_BUNDLED_PLUGINS=$WT/plugins $WT/.venv/bin/hermes coworker compose $SCN/spec/coworker-types.yaml --out $ROOT/render > $ART/scenario-$AC/host-compose.log 2>&1 || { echo "host compose failed: FAIL" >&2; exit 1; }
   cp $ROOT/render/default/policy-gateway.yaml $ROOT/policy-gateway.yaml
   grep -q 'host: inference.local' $ROOT/policy-gateway.yaml || { echo "rendered gateway policy lacks inference.local: FAIL" >&2; exit 1; }
   ```
   Nothing is read from the OSH-F64 lane's `…/osh-f64/policy-gateway.yaml` any more: that file is the OSH-F64
   posture, not the gated render.
2. **Provision and onboard the fleet as the parent's `## Setup` does**, with the prefix and root substitutions,
   the gated spec, every in-sandbox command run through `sx`, and the cycle-2 gates below:
   - `openshell sandbox create --name osh-f64c-gw --from osh-f64-gateway:pinned --policy $ROOT/policy-gateway.yaml`
     (the image tag is the lane's pinned GATEWAY image, rebuilt at this head, not a sandbox name).
     Then confirm the home resolves:
     `sx 'echo "home=$HERMES_HOME py=$PY"; ls -d "$HERMES_HOME" && "$PY" -c "print(1)"' | tee $ART/scenario-$AC/env.txt` →
     expect `home=/sandbox/.hermes`, the directory listed, and `1`.
     Then stage the committed gated spec and the helpers, which serve's readiness wait below already reads:
     `stage $SCN/spec '"$HERMES_HOME/.osh-f64c"'`; `stage $SCN/helpers '"$HERMES_HOME/.osh-f64c"'`. Every `<SPEC>`
     below is `$SPEC` in a DOUBLE-quoted `sx` command: `$SPEC` expands on the tester side to the literal text
     `"$HERMES_HOME/.osh-f64c/spec/coworker-types.yaml"`, whose `$HERMES_HOME` then expands in the sandbox. Inside
     those double quotes, write every other in-sandbox `$` as `\$`.
   - `REF` is the PR head under test, resolved at run time. Phase-a and the install both install the fleet plugins
     from the fork at `--ref $REF`, so the image's fork mirror (`/opt/hermes/fork.git`, observed on the lane image) must
     carry it; if it does not, the lane image predates this head and the run stops as `FAIL(env)`:
     ```bash
     REF=$(git -C "$WT" rev-parse HEAD)
     sx "git -C /opt/hermes/fork.git cat-file -e $REF^{commit}" || { echo "image mirror lacks $REF: FAIL(env)" >&2; exit 1; }
     ```
   - **P1 — the OpenShell Sandbox CA** (`fleet-openshell.md:516-518`). Record both CA files the lane is known to
     place: the doc's bundle and the CA file OSH-F64.b's Part B checked. Then record the CA env names a fleet process
     starts with (Hermes resolves them in this order before certifi, release `agent/ssl_verify.py:30-63`, used by the
     provider client, `agent/agent_runtime_helpers.py:2733-2735`):
     ```bash
     sx 'for f in /etc/openshell-tls/openshell-ca.pem /etc/openshell-tls/ca-bundle.pem; do if [ -s "$f" ]; then echo "$f size=$(stat -c %s "$f") sha256=$(sha256sum "$f" | cut -d" " -f1) certs=$(grep -c "BEGIN CERTIFICATE" "$f")"; else echo "$f absent"; fi; done' | tee $ART/scenario-$AC/p1-ca.txt
     grep -q '^/etc/openshell-tls/ca-bundle.pem .* certs=[1-9]' $ART/scenario-$AC/p1-ca.txt || { echo "P1: no OpenShell Sandbox CA bundle: FAIL(env)" >&2; exit 1; }
     ```
     Both paths are recorded either way; `openshell-ca.pem` absent alone does not fail P1.
     The CA env names (names and paths only) are printed by P3's probe, from the fleet start path. P3 is the
     functional proof: a TLS handshake to `inference.local` that succeeds has trusted the OpenShell Sandbox CA,
     whatever file carries it.
   - Parent step 1, the managed-dir scan guard. `HX` already exports `HERMES_MANAGED_DIR`:
     `sx 'mkdir -p "$HERMES_MANAGED_DIR" && cp /tmp/osh-lane/managed/config.yaml "$HERMES_MANAGED_DIR/"'`.
   - Parent step 2: the broker host-policy mirror. Phase-a with the lane-local spec, from the image's own copy of the
     fleet plugin (observed on the lane image), then compose:
     ```bash
     sx "cd \"\$HERMES_HOME/plugins/nv-coworker-compose/openshell\" && \"\$PY\" installer.py phase-a --spec $SPEC --ref $REF"
     sx "hermes coworker compose $SPEC --out \"\$HERMES_HOME/.osh-f64c/render\""
     ```
     Mirror each role's policy with `mkdir -p $ROOT/render/<role>` and
     `sx 'cat "$HERMES_HOME/.osh-f64c/render/<role>/policy-<role>.yaml"' > $ROOT/render/<role>/policy-<role>.yaml`,
     for every role the installer provisions. This overwrites the step-1 host render's role policies with
     byte-identical content (compose's policy output is deterministic), so `--policy-root $ROOT` reads the same files
     either way. Then bind the host render to the image's own compose:
     ```bash
     H=$(sha256sum $ROOT/policy-gateway.yaml | cut -d' ' -f1)
     S=$(sx 'sha256sum "$HERMES_HOME/.osh-f64c/render/default/policy-gateway.yaml"' | cut -d' ' -f1)
     echo "policy-gateway.yaml host=$H sandbox=$S" | tee -a $ART/scenario-$AC/evidence.txt
     [ -n "$H" ] && [ "$H" = "$S" ] || { echo "gateway policy host/sandbox mismatch (image plugin != head): FAIL(env)" >&2; exit 1; }
     ```
   - Parent step 3, on the Setup-5 start path (operator msg 508). The token is generated at run time and exported in the SAME `sx` call that starts `hermes serve`
     (a separate remote shell would not keep it). `serve` listens on its release defaults, `127.0.0.1:9119`
     (`hermes_cli/subcommands/dashboard.py:26-31`), which the dashboard's `$P` does not collide with:
     ```bash
     TOK=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
     sx "$GENV export HERMES_DASHBOARD_SESSION_TOKEN=$TOK; setsid nohup hermes serve --host 127.0.0.1 --port 9119 > \"\$HERMES_HOME/osh-f64c-serve.log\" 2>&1 < /dev/null &"
     sx -t 300 "until \"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/health_ok.py\" 9119; do sleep 2; done"
     GW_URL="ws://127.0.0.1:9119/api/ws?token=$TOK"
     ```
   - The install, on the Setup-5 start path (operator msg 508), from the same image copy of the fleet plugin, with the `REF` resolved above. It runs in the
     BACKGROUND and is polled, as the parent lane ran it. The image has no service manager, so the installer's final
     `hermes gateway restart` runs the gateway in the foreground (release `hermes_cli/gateway.py:8888-8896`, `:6426`)
     and the install never returns, while the broker cuts any one exec at about 300 s. The wrapper records the
     install's exit code in `osh-f64c-install.rc` if it ever exits:
     ```bash
     W='w() { f="$HERMES_HOME/gateway_state.json"; [ -e "$f" ] || { echo absent; return; }; "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[\"pid\"], d[\"start_time\"], d[\"gateway_state\"])" "$f" 2>/dev/null || echo ambiguous; };'
     BASE=$(sx "$W"' touch "$HERMES_HOME/osh-f64c-install.start"; sleep 1; for k in 1 2 3 4 5; do b=$(w); [ "$b" != ambiguous ] && break; sleep 1; done; echo "$b"') || BASE=ambiguous
     echo "baseline: $BASE" | tee $ART/scenario-$AC/install-poll.txt
     [ "$BASE" != ambiguous ] || { echo "baseline gateway_state.json unreadable: FAIL" >&2; exit 1; }
     B0=$(echo "$BASE" | cut -d' ' -f1,2)
     sx "$GENV setsid nohup sh -c '\"\$0\" \"\$@\"; echo \$? > \"\$HERMES_HOME/osh-f64c-install.rc\"' \"\$HERMES_HOME/plugins/nv-coworker-compose/openshell/install-into-sandbox.sh\" $SPEC --ref $REF --gateway-url '$GW_URL' --policy-root $ROOT > \"\$HERMES_HOME/osh-f64c-install.log\" 2>&1 < /dev/null &"
     RC='if [ -s "$HERMES_HOME/osh-f64c-install.rc" ]; then echo "exited rc=$(cat "$HERMES_HOME/osh-f64c-install.rc")"; exit 0; fi;'
     POLL="i=0; while [ \$i -lt 70 ]; do $RC"
     POLL+=' if [ "$HERMES_HOME/gateway_state.json" -nt "$HERMES_HOME/osh-f64c-install.start" ]; then s=$(w); [ -e "$HERMES_HOME/osh-f64c-install.first-state.json" ] || cp "$HERMES_HOME/gateway_state.json" "$HERMES_HOME/osh-f64c-install.first-state.json";'
     POLL+=" case \"\$s\" in ambiguous|absent) ;; *' running') if [ \"\${s% *}\" != \"\$B0\" ]; then $RC echo \"ready \$s\"; exit 0; fi;; esac; fi;"
     POLL+=' sleep 3; i=$((i+1)); done; echo waiting'
     INSTALL=waiting
     for slice in 1 2 3 4 5 6 7 8 9 10; do
       INSTALL=$(sx -t 240 "$W B0='$B0'; $POLL") || INSTALL=waiting
       echo "slice $slice: $INSTALL" | tee -a $ART/scenario-$AC/install-poll.txt
       [ "${INSTALL%% *}" = waiting ] || break
     done
     sx -t 60 'cat "$HERMES_HOME/osh-f64c-install.first-state.json" 2>/dev/null || echo none' | sed 's/^/first post-start gateway_state.json: /' >> $ART/scenario-$AC/install-poll.txt
     sx -t 60 'cat "$HERMES_HOME/osh-f64c-install.log"' > $ART/scenario-$AC/install.log
     { if [ "${INSTALL%% *}" = ready ]; then echo 'restart_exit=pending (foreground gateway running)'; else echo "restart_exit=none (install $INSTALL)"; fi
       cat $ART/scenario-$AC/install.log; } > $ART/scenario-$AC/gateway-restart.log
     [ "${INSTALL%% *}" = ready ] || { echo "install not ready ($INSTALL): FAIL" >&2; tail -40 $ART/scenario-$AC/install.log >&2; exit 1; }
     ```
     The broker rejects any exec argument that contains a newline, so the poll body is assembled into `POLL` as ONE line.
     Before the launch, `BASE` records the writer `(pid, start_time)` of any existing `gateway_state.json`, or `absent`;
     every write stamps its writer's own pair (release `gateway/status.py:1206-1209`, `:666-669`). Ready means the file is
     newer than the start marker (the one-second gap keeps `-nt` exact on whole-second timestamps), reports
     `"gateway_state":"running"`, and names a writer other than `BASE`. A snapshot that does not parse is inconclusive,
     never ready. The exit marker is checked before the status is read and again after it: once the install has exited,
     its in-process gateway has exited with it, so `exited rc=<n>` fails Setup whatever the code. So does the overall
     bound expiring: ten slices of at most 240 s, about 40 min, well above the parent lane's install, which was still
     running 13 min after launch. `install-poll.txt` records the baseline, each slice and the first post-start snapshot.
     The launch
     argument is double-quoted on purpose: `$SPEC`, `$REF`, `$GW_URL` and `$ROOT` are tester-side values, and the single
     quotes keep the URL's `?token=` intact in the sandbox shell.
   - **P2 — no `API_SERVER_*` in a profile `.env`** (`fleet-openshell.md:521-522`: the multiplexed gateway skips a
     profile whose `.env` carries one). Names only:
     `sx 'grep -l "^API_SERVER_" "$HERMES_HOME/.env" "$HERMES_HOME"/profiles/*/.env 2>/dev/null; true' | tee $ART/scenario-$AC/p2-env.txt`
     → expect no output; a listed file is `FAIL(env)`.
   - **No grant preflight.** Under the gated render the §D7.1 `profile_secret_sets` grants are not written
     (`compose.py:4164-4167`), there is no OneCLI identity to grant, and `onecli-onboard` is not run.
   - The post-install onboard, `sx "hermes -p default onboard coworker $SPEC --gateway-url '$GW_URL'"`, asserting
     that `orchestrator` and `builder` each hold a canonical hidden `Bot Chat` plus a `ui_meta['hermes-bots']`
     entry. Then confirm the served configs still carry the gated route and no OneCLI hop (names and booleans only):
     ```bash
     for r in orchestrator builder; do sx "\"\$PY\" -c \"import yaml,sys; c=yaml.safe_load(open(sys.argv[1])); p=(c.get('providers') or {}).get('compatible-endpoint') or {}; print(sys.argv[2], 'provider', (c.get('model') or {}).get('provider'), 'base_url', p.get('base_url'), 'placeholder', p.get('api_key') == 'sk-OPENSHELL-PROXY-REWRITE', 'header', (p.get('extra_headers') or {}).get('X-Hermes-Profile'), 'podman_onecli', 'podman-onecli' in ((c.get('plugins') or {}).get('enabled') or []))\" \"\$HERMES_HOME/profiles/$r/config.yaml\" $r"; done | tee $ART/scenario-$AC/served-config.txt
     ```
     → expect, for each role, `provider compatible-endpoint base_url https://inference.local/v1 placeholder True
     header osh-f64c-<role> podman_onecli False`; anything else FAILS Setup.
   - **Setup 8a — the per-worker ssh config** (`fleet-openshell.md:527-531`, precondition 5). The installer's
     `provision_sshconfig` step only prints each `openshell sandbox ssh-config` block to its log
     (`plugins/nv-coworker-compose/openshell/installer.py:535`, `:755-759`), and nothing writes it into the gateway
     user's OpenSSH config, so the profile's `terminal.ssh_host` alias would not resolve. Append each worker
     sandbox's block, once, to `~/.ssh/config` inside `osh-f64c-gw` (a file write, so `sx` is fine), then record
     the `Host` lines and the file's sha256:
     ```bash
     sx 'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && for r in orchestrator architect builder tester reviewer; do grep -qx "Host openshell-osh-f64c-$r" "$HOME/.ssh/config" 2>/dev/null || openshell sandbox ssh-config "osh-f64c-$r" >> "$HOME/.ssh/config" || exit 1; done; chmod 600 "$HOME/.ssh/config"; grep "^Host " "$HOME/.ssh/config"; sha256sum "$HOME/.ssh/config"' | tee $ART/scenario-$AC/ssh-config.txt
     grep -qx 'Host openshell-osh-f64c-orchestrator' $ART/scenario-$AC/ssh-config.txt && grep -qx 'Host openshell-osh-f64c-builder' $ART/scenario-$AC/ssh-config.txt || { echo "ssh config lacks a worker Host block: FAIL" >&2; exit 1; }
     ```
     The five roles are the ones the installer provisions from the spec. P4 then proves the alias works.
   - **G1 — the static route check, before any model call** (key names and booleans only, never a value). The live
     gateway policy is the host file the sandbox was created from (its hash matched the in-sandbox render above);
     every installed config is checked against the staged spec's `egress.inference_provider`:
     ```bash
     $WT/.venv/bin/python $SCN/helpers/route_static.py policy $ROOT/policy-gateway.yaml $SCN/spec/coworker-types.yaml | tee $ART/scenario-$AC/g1-static.txt
     sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/route_static.py" configs "$HERMES_HOME/.osh-f64c/spec/coworker-types.yaml"' | tee -a $ART/scenario-$AC/g1-static.txt
     [ "$(grep -c '^route_static_ok yes' $ART/scenario-$AC/g1-static.txt)" = 2 ] || { echo "G1 static route check: FAIL" >&2; exit 1; }
     ```
     The policy's only REST endpoint must be `inference.local:443` with exactly the four rules, and its only raw hop
     the spec's 18777 broker. Each of `default` and the five served roles must carry provider `compatible-endpoint`,
     `base_url` `https://inference.local/v1`, `api_mode` `chat_completions` and the spec's placeholder (by equality),
     `X-Hermes-Profile: osh-f64c-<role>` (`osh-f64c-default` for the default store), and none of `custom_providers`,
     `fallback_model`, `fallback_providers`, `delegation.{provider,base_url,api_key}`,
     `auxiliary.*.{provider,base_url,api_key}`, `secrets.onecli`, or `podman-onecli` (entries or enabled). The
     installer edits the default config in place and never writes its provider block
     (`plugins/nv-coworker-compose/openshell/installer.py:262-340`), so the `osh-f64c-gateway` seed carries the
     default render's header itself.
   - The parent's `gateway-restart.log` capture is written by the block above: while the gateway runs, the restart has
     no exit code yet, so the file records `restart_exit=pending` followed by the install's output (on a failed install,
     `restart_exit=none` and the install's state).
3. **Stage and confirm the PR head's nv-bot-chat** in the gateway's user plugins (the installer installs only
   the fleet plugins): `stage $WT/plugins/nv-bot-chat '"$HERMES_HOME/plugins"'` (the helpers were staged in step 2);
   then `sx 'grep -A6 "^plugins:" "$HERMES_HOME/config.yaml" | grep -q nv-bot-chat || hermes -p default plugins enable nv-bot-chat'`.
4. **Dashboard and the ONE forward**, as AC-OSH-F64-5 Setup steps 6–8:
   - Start the dashboard on the Setup-5 start path (operator msg 508): `sx "$GENV export HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist; setsid nohup hermes dashboard --host 127.0.0.1 --port $P --no-open --skip-build > \"\$HERMES_HOME/osh-f64c-dashboard.log\" 2>&1 < /dev/null &"`.
     `--skip-build` is needed because the sandbox has no npm egress, and `HERMES_WEB_DIST` because the image's
     prebuilt SPA is not at the default `PROJECT_ROOT/hermes_cli/web_dist` (see AC-OSH-F64-5 Setup 6).
   - Wait until `/api/health` reports `"ok": true`:
     `sx -t 300 "until \"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/health_ok.py\" $P; do sleep 2; done"`.
   - Open the ONE forward exactly as AC-OSH-F64-5 Setup 7 does: log the command to `forwards.txt`, start it in the
     background (`> $ART/scenario-$AC/forward-start.log 2>&1 &`, pid to `forward-client.pid`), then wait up to 300 s
     until the colour-stripped `openshell forward list` shows the `osh-f64c-gw 172.17.0.1 $P <pid> running` row,
     with the same `sed` + `grep -E` (saved to `forward-list.txt`). A timeout is a
     Setup failure; the start is never retried.
   - Bridge the Host guard from loopback: `socat TCP-LISTEN:$P,fork,reuseaddr,bind=127.0.0.1 TCP:172.17.0.1:$P & echo $! > $ART/scenario-$AC/socat.pid`.
   - `URL=http://127.0.0.1:$P/`.
   - The session token for API reads is `T=$(curl -s $URL | grep -o '__HERMES_SESSION_TOKEN__="[^"]*"' | cut -d'"' -f2)`.
5. **The dashboard PID and its netns, then P3, P4 and the G3 self-test, all before step 1.**
   `netns_record.py` lists the fleet processes; the dashboard PID is the lowest `dashboard` line (a `hermes` →
   `hermes.real` wrapper chain is one ancestry line, and every PTY child descends from its lowest PID):
   ```bash
   sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/netns_record.py"' | tee $ART/scenario-$AC/netns.txt
   DPID=$(awk '$1=="dashboard"{print $2}' $ART/scenario-$AC/netns.txt | sort -n | head -1)
   DNS=$(awk -v p="$DPID" '$1=="dashboard" && $2==p {print $3}' $ART/scenario-$AC/netns.txt)
   [ -n "$DPID" ] && [ -n "$DNS" ] && [ "$DNS" != unreadable ] || { echo "no dashboard PID/netns: FAIL" >&2; exit 1; }
   echo "dashboard $DPID $DNS" | tee -a $ART/scenario-$AC/evidence.txt
   ```
   Each probe below runs on the Setup-5 start path (`bgrun`, guard env set); its own `netns` line must equal `$DNS`,
   and a non-zero exit stops Setup. Define the `## Steps` helper block (`listing` … `win_close`) now, before P3: the
   `setup` route window opens here.
   - **P3 — the managed route from that netns:**
     `bgrun p3 '"$PY" "$HERMES_HOME/.osh-f64c/helpers/inference_probe.py" default' | tee $ART/scenario-$AC/p3-probe.txt || exit 1`
     → expect `status 200 models <n>`, `rc=0` and `netns $DNS`. Anything else (TLS failure, 401, connect error) is
     `FAIL(env)`, recorded with its error class, and the scenario stops. It is a `GET /v1/models`, not a model call.
   - **P4 — gateway → builder over the ssh alias:**
     `bgrun p4 'ssh -o BatchMode=yes openshell-osh-f64c-builder hostname && echo netns $(readlink /proc/self/ns/net)' | tee $ART/scenario-$AC/p4-ssh.txt || exit 1`,
     then broker-side `openshell sandbox exec osh-f64c-builder -- hostname | tee -a $ART/scenario-$AC/p4-ssh.txt || exit 1`
     → expect `rc=0` and the same hostname twice. A failure is `FAIL(env)` naming the denied binary, never patched here.
   - **G3 self-test (re-run on the lane; the builder's local TLS-stub run is unit evidence only):**
     `bgrun g3self '"$PY" "$HERMES_HOME/.osh-f64c/helpers/guard_selftest.py"' | tee $ART/scenario-$AC/g3-selftest.txt || exit 1`
     → expect `armed yes`, `netns $DNS`, `selftest rejected`, `rc=0`. G4 then confirms no router record for it.

## Steps

Drive each step as the parent scenario's step of the same number does, except where stated. Then capture it
from the nv-bot-chat tab on the one forwarded `$URL`, using agent-browser (bounded waits, one screenshot per
step). Before each evidence query, re-read the listing (step 0's `curl`) and use that profile's current
`resolved_id` (`<tip>`); a compression mid-run moves the tip.

```bash
NONCE=$(python3 -c 'import secrets; print(secrets.token_hex(4))'); echo "nonce $NONCE" >> $ART/scenario-$AC/evidence.txt
listing() { curl -s -H "Authorization: Bearer $T" ${URL}api/plugins/nv-bot-chat/bot-chats > $ART/scenario-$AC/bot-chats.json; }
field() { python3 -c "import json,sys; d={r['profile']: r for r in json.load(open(sys.argv[1]))['bot_chats']}; print(d[sys.argv[2]][sys.argv[3]])" $ART/scenario-$AC/bot-chats.json "$1" "$2"; }
row() {  # row <profile> <session> <role> [tool_name] [needle] -> evidence.txt; %q keeps an empty tool_name as ''
  printf -v quoted_args ' %q' "$@"
  sx "\"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/evidence_row.py\"$quoted_args" | tee -a "$ART/scenario-$AC/evidence.txt"
}
expect_id() {  # G2: the id the PTY route will set, computed the route's own way right before a connect
  sx "\"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/pty_select.py\" expect $1 $2" | awk '$1=="expected_id"{print $2}'
}
pty_child() {  # G2 selector: exactly one dashboard-descended tui_gateway child with that id + profile
  sx "\"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/pty_select.py\" select $DPID $1 $2" | tee $ART/scenario-$AC/pty-select-$3.txt
  [ "$(awk '$1=="pty_matches"{print $2}' $ART/scenario-$AC/pty-select-$3.txt)" = 1 ] && grep -q " netns $DNS dashboard_netns $DNS$" $ART/scenario-$AC/pty-select-$3.txt || { echo "G2 $3: not exactly one PTY child in the dashboard netns: FAIL" >&2; exit 1; }
}
usage_snap() { sx "\"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/route_record.py\" $T0 $ROUTE" > $ART/scenario-$AC/usage-$1.txt || true; }
win_open() { eval "W0_$1=$(sx 'date +%s.%N')"; usage_snap $1-before; }
win_close() {  # G4 for window $1: stop at the first unmatched record
  local w0; eval "w0=\$W0_$1"; W1=$(sx 'date +%s.%N'); usage_snap $1-after
  sx 'tar -C "$HERMES_HOME/.osh-f64c" -czf - guard | base64 -w0' | base64 -d | tar -C $ART/scenario-$AC -xzf -
  # openshell.log: the tester's `openshell logs` capture for osh-f64c-gw covering [w0, W1] (OSH-F64.b Part B format)
  python3 $SCN/helpers/route_reconcile.py $ART/scenario-$AC/guard $ART/scenario-$AC/openshell.log \
    $ART/scenario-$AC/usage-$1-before.txt $ART/scenario-$AC/usage-$1-after.txt "$w0" "$W1" | tee $ART/scenario-$AC/route-reconcile-$1.txt \
    || { echo "G4 $1: route reconcile: FAIL" >&2; exit 1; }
}
```

**Route gates on every turn (ADR eeb30a42 G2–G4).** Run `win_open setup` before Setup 5's P3, and `win_close setup`
after the G3 self-test: the probes' `GET`s are recorded, and the self-test's rejected `POST` must have no router
record. Then for each step `n` in 1–5, `win_open s<n>` before driving it and `win_close s<n>` after its evidence
row; at the end, `W0_all=$W0_setup` and `win_close all` reconciles the whole run. `route_reconcile.py` matches by
time order plus method+path within ±2 s (router lines carry no PID). It records the first probe pair's skew and
fails on: a probe that is not 200 or has no router `GET /v1/models`; an allowed `POST` with no router record, or one
routed before its PID's probe; a rejected `POST` the router saw; a router `POST /v1/chat/completions` that no guard
record claims; or a store whose 2xx guard `POST`s differ from its `api_call_count` increment (`.hermes` is the
`default` store). A turn counts only if its `route-reconcile-s<n>.txt` has `allowed` lines from the PIDs that turn
used: the G2 PTY child for steps 1–3, the delivery children for steps 3–4, the step-5 CLI for step 5. Before each
`win_close`, save the `openshell logs` capture for `osh-f64c-gw` covering the window as `openshell.log` (the format
OSH-F64.b Part B recorded: `[<epoch>] … routing proxy inference request … method=<M> path=<P>`).

0. **The canonical ids.** Run `listing`; `ORCH_SID=$(field orchestrator session_id)`; `BUILD_SID=$(field builder session_id)`.
   Then `echo step-0 >> evidence.txt; row orchestrator $ORCH_SID assistant` → expect: the first line reads
   `session <ORCH_SID> hidden=1 title=Bot Chat` (the hidden canonical row, not a fresh session). Every later
   `row` call gets the tip; the helper walks `parent_session_id` up from it and prints the canonical ancestor on
   that line. Run
   `row builder $BUILD_SID assistant` → expect the same `hidden=1 title=Bot Chat` shape for the builder.
1. **Human → the orchestrator's canonical Bot Chat.** In agent-browser, select `orchestrator` in the
   dashboard profile combobox → expect the `orchestrator »` prompt. Then open `${URL}chat?profile=orchestrator&resume=$ORCH_SID`,
   keeping `?profile=` in the URL because the SPA's profile scope is initialised from it
   (`web/src/contexts/ProfileProvider.tsx:37-45`). Use the same URL whenever you return to the chat in steps 2–3.
   The SPA forwards both to `/api/pty?profile=orchestrator&resume=$ORCH_SID` (`web/src/pages/ChatPage.tsx:1165,1174`).
   A bare `?profile=` without `resume` can open the active-session file or a fresh session instead
   (`hermes_cli/web_server.py:17475-17494`), and `message_agent` exists only in the canonical Bot Chat.
   **G2, immediately before that connect:** `EXP=$(expect_id orchestrator $ORCH_SID)`, the route's own
   `_session_latest_descendant(canonical_id, db)[0] or canonical_id` over the profile's `state.db` opened read-only
   (`hermes_cli/web_server.py:16623-16634`; not the listing's `resolved_id`). Once the chat page has connected, and
   before sending, run `pty_child orchestrator $EXP s1` → exactly one PID in `$DNS`. Every reconnect (a page reload,
   a re-opened chat URL) recomputes `EXP` and re-runs `pty_child`; a new child must also log its own guard `probe`.
   Send the parent's step-1 message, `run change P7-$NONCE`, over that SAME connection, and wait (bounded) for the
   orchestrator's reply. Then open the **Bot Chat** tab and click `orchestrator` → expect: the human line and the
   orchestrator's acceptance turn are visible → `step-1.png`. Evidence: `echo step-1 >> evidence.txt; row orchestrator <tip> assistant`.
   Steps 2–3 continue in this same resumed chat and child; `win_close s<n>` must show `allowed` lines from that PID.
   If the browser reconnects in steps 1–3, recompute `EXP` immediately before the reconnect and, once connected,
   run `pty_child orchestrator $EXP s<n>r<k>` (`k` counts reconnects in that step).
2. **Unwired `message_agent` → builder.** Drive the orchestrator to message the builder before any wire, as
   parent step 2 does. In the tab, click **← All Bot Chats**, then `orchestrator` → expect: a `tool` row
   carrying the wiring-gate refusal → `step-2.png`. Evidence: `echo step-2 >> evidence.txt; row orchestrator <tip> tool message_agent`.
3. **Wire, then deliver.** Run `sx 'hermes wire add orchestrator builder'`,
   then ask the orchestrator in the same chat to send to the builder again → refresh `orchestrator` in the tab →
   expect: a newer `tool` row carrying the delivery confirmation → `step-3.png`. Evidence: `echo step-3 >> evidence.txt;
   row orchestrator <tip> tool message_agent '"status": "sent"'`. The needle selects the delivery RESULT by its content: a
   successful send returns `{"status": "sent", "to": …}` (release `tools/bot_mode_dm.py:705-714`), while step 2's refusal
   is an `{"error": …}` payload (`:233-238`). Its `row <msg-id>` must also be newer than step 2's.
4. **The builder's delivery turn.** This is parent step 4 with the prefix substitution: a `write_file` plan to
   `.hermes/plans/builder.md`, then the `terminal` change inside `osh-f64c-builder`, writing
   `/tmp/osh-f64c-builder.marker`. The delivery child runs `-c "Bot Chat"` (`tools/bot_mode_dm.py:361-378`).
   Open the tab, **← All Bot Chats**, then `builder` → `timeout 600 agent-browser wait --text "P7-BUILT:$NONCE"` →
   expect: the builder's `P7-BUILT:<nonce>` reply in its canonical Bot Chat → `step-4.png`. Evidence:
   `echo step-4 >> evidence.txt; row builder <tip> assistant "" "P7-BUILT:$NONCE"`. The parent's routing and
   isolation probes may be recorded too, but they are not graded here.
5. **The veto, in the builder's Bot Chat.** Run the worker turn inside `osh-f64c-gw` through the local-teammate
   transport (`tools/bot_mode_dm.py:31-33`), so the attempt lands in that Bot Chat. It is a model turn, so it starts
   on the Setup-5 background start with the guard env (operator msg 508), never as a foreground `sx`. Writing the
   query file makes no network call and stays a plain `sx`:
   `sx 'printf "%s\n" "Use the cronjob_manage tool to create an hourly cron job named osh-f64c-probe that says hello." > /tmp/osh-f64c-q5.txt'`;
   then `bgrun s5 'hermes -p builder chat --in ~ -c "Bot Chat" -Q --query-file /tmp/osh-f64c-q5.txt' | tee $ART/scenario-$AC/step-5-cli.txt`.
   Then refresh `builder` in the tab → expect: a `tool` row carrying the `cronjob_manage` denial that names
   `orchestrator-only` → `step-5.png`. Evidence: `echo step-5 >> evidence.txt; row builder <tip> tool cronjob_manage orchestrator-only`.
6. **Teardown.** `kill $(cat $ART/scenario-$AC/socat.pid)`; `kill $(cat $ART/scenario-$AC/forward-client.pid) 2>/dev/null`; then delete every `osh-f64c-*` sandbox and policy
   (`openshell sandbox delete osh-f64c-gw`, `osh-f64c-orchestrator`, `osh-f64c-builder`, plus their policies
   as the parent's teardown does). Nothing outside `osh-f64c-*` and `$ROOT` is touched.

## Pass

All five events are visible in the canonical hidden Bot Chats through the nv-bot-chat tab, on the one
forwarded dashboard URL: steps 1–3 in the orchestrator's, 4–5 in the builder's. Each screenshot matches a
`state.db` row in `evidence.txt`. A step whose expected event never happens leaves nothing to capture, and
FAILS the criterion. AC-4's other behaviour gates are not re-graded here. That bounds this criterion's scope
(evidence clause only); it is not a carried PASS. **Cycle-2 grading rule (operator msg 398):** every gate this
criterion carries (P1–P4, G1–G4, the five screenshots and their rows) is graded on current-run evidence only, and
no earlier PASS or artifact satisfies one.

## Evidence

All files are under `$ART/scenario-AC-OSH-F64-4/`:

- `step-1..5.png`: tab screenshots, one per step.
- `evidence.txt`: the nonce and, per step, the `evidence_row.py` output (stdlib `sqlite3`; the coworker image
  has no `sqlite3` CLI; columns `role` / `content` / `tool_name`, `hermes_state_common.py:459-463`). Each step's
  output is a `session … hidden=1 title=Bot Chat` line plus the matching row:
  - step 1: an `assistant` row;
  - steps 2 and 3: `tool` rows with `tool_name` `message_agent`, step 3's newer and containing `"status": "sent"`;
  - step 4: an `assistant` row containing `P7-BUILT:<nonce>`;
  - step 5: a `tool` row with `tool_name` `cronjob_manage` whose content names `orchestrator-only`.
  A `row none` on any step is a FAIL for that step.
- **One forward:** `forwards.txt` holds exactly one `openshell forward start $P osh-f64c-gw -d`.
- **One gateway:** append `sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/gateway_count.py"'`
  to `evidence.txt`. It must print `gateway_processes 1`; the `serve_processes` line it also prints is recorded,
  not gated.
- **Call / $ totals** within the per-scenario cap (40 calls / $5). Append the stdlib totals over the gateway's
  default, orchestrator and builder stores to `evidence.txt`:
  `sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/usage_totals.py"' | tee -a $ART/scenario-$AC/evidence.txt` → `calls <n> usd <x>`.
- **Route record (cycle 2, single authority).** P1–P4 (`p1-ca.txt`, `p2-env.txt`, `p3-probe.txt`,
  `p4-ssh.txt`), the two `policy-gateway.yaml` hashes (in `evidence.txt` from Setup), and every
  `session_model_usage` row of every session started during this scenario: the delivery children, aux tasks such
  as title generation and every other session count, not only the two canonical Bot Chats. Each row is recorded with
  `session_id`, `task`, `billing_base_url` and `api_call_count` (columns `hermes_state_common.py:483-499`;
  `billing_base_url` is written from the live client's `base_url`, `agent/conversation_loop.py:4500`):
  `sx "\"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/route_record.py\" $T0 $ROUTE" | tee $ART/scenario-$AC/route-record.txt`.
  It covers the gateway/default store and every `profiles/*/state.db`, and must print `route_ok yes` and
  `controls_ok yes`, exiting 0. The two positive controls are the orchestrator store and the builder store each
  holding a row with `api_call_count > 0`. Any row with `api_call_count > 0` whose `billing_base_url` is not
  `https://inference.local/v1` means a model call left the managed route, and the run FAILS whatever the screenshots
  show; a missing positive control FAILS too.
- **Network namespaces** (recorded, read with the route record): `netns.txt`, from
  `sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/netns_record.py"'` at the end of Setup and again after step 5
  (append). Every guard record's `netns` must equal the dashboard's `$DNS`.
- **Route gates:** `p3-probe.txt`, `p4-ssh.txt`, `g1-static.txt`, `g3-selftest.txt`, `pty-select-s1*.txt` (one per connect),
  `route-reconcile-{setup,s1..s5,all}.txt` (each must end `reconcile_ok yes`), `usage-*-{before,after}.txt`,
  the copied `guard/` dir (`<pid>.armed` + `<pid>.jsonl`) and `openshell.log`. Every model-emitting PID in
  `route-reconcile-all.txt` must have an `.armed` marker and a `probe` 200 matched to a router `GET /v1/models`
  before its first counted `POST`; a PID without them is `FAIL(env)`.
- Evidence holds names, hashes, ids and status codes only: never a header value, a token or a key literal.
- `bot-chats.json` (the last listing), plus the parent's `gateway-restart.log`.
