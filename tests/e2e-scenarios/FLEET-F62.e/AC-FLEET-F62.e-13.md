---
ac: AC-FLEET-F62.e-13
kind: live
model: live
base_url: https://inference.local/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures: []
LIVE_MODEL_CALLS_MAX: 4
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-FLEET-F62.e-13: the sandbox CA survives a sandbox restart (live)

In a real OpenShell gateway sandbox, a Hermes process completes a TLS handshake to the OpenShell
inference route through the sandbox proxy, both before and after a sandbox restart that re-mints
the CA. No file is edited between the two. A stale-copy control fails the handshake after the
restart.

**Route (erratum E6, Orchestrator ruling 22f0ef87).** Both model turns and the step-5 control use
the OpenShell managed inference route `inference.local:443`, through the same sandbox proxy
tunnel. That route's TLS is signed by the sandbox CA, so it is the handshake a re-mint breaks. The
external inference host is refused by the lane gateway policies.

`model: live`: `base_url` `https://inference.local/v1`, `api_mode` `chat_completions`, and
`model_id` is the model the route serves, as listed by the readiness probe (ruling item 3). There
is no forced `tool_choice`. The harness supplies its non-secret sentinel for the provider key
field, and the route's operator-configured provider supplies the credential. At most 4 model
calls (two turns, one retry headroom each). This is not agent-to-agent: one profile, `triager`.

## Gating

- **Restart verb.** Step 2 runs `openshell sandbox restart fleet-f62d-e13gw` (lane verb, allowed for
  `fleet-f62d-*` only). It is a docker restart of that one sandbox container: it keeps the
  filesystem and the home, re-mints the CA, and returns once the restart completes.
  Delete-and-recreate is not a restart. The scenario still requires an observed re-mint (step 3).
- **Image and policy.** `fleet-f62d-e13gw` is created from the per-head lane image named in the
  Orchestrator's LANE READY, with the gateway policy `nanoclaw-base/render/default/policy-gateway.yaml`
  (sha256 `9e7a2c85…`, the F62 fleet's own render), which allows `inference.local:443`. Record
  `sha256sum` of that policy file in `$EV` when the sandbox is created.

## Fixtures

None installed by the harness (`fixtures: []`). The `triager` distribution comes from the
PR-head render of `tests/e2e-scenarios/FLEET-F62.c/spec/openshell/` (the fleet spec of record),
which carries `egress.openshell_trust.bundle: /etc/openshell-tls/ca-bundle.pem`. The product spec
and fixtures are not changed for this scenario.

## Setup

Everything below runs inside the one sandbox, `fleet-f62d-e13gw`. `$SHA` is the full 40-character
PR head; `$SPEC` is the in-image checkout of
`tests/e2e-scenarios/FLEET-F62.c/spec/openshell/coworker-types.yaml` at `$SHA`; `$GW` is the
sandbox's gateway home (`$HERMES_HOME` of the gateway process). Every recorded value goes to
`$EV`. Any `FAIL(env)` below stops the run, uncounted, and goes back to the Orchestrator.

```bash
EV=$PWD/scenario-AC-FLEET-F62.e-13/evidence.txt; mkdir -p "${EV%/*}"
for p in /opt/hermes/.venv/bin/python /opt/hermes/venv/bin/python python3; do
  "$p" -c 'import hermes_cli' 2>/dev/null && HPY=$p && break; done
[ -n "${HPY:-}" ] || { echo "FAIL(env): no Hermes interpreter" >> "$EV"; exit 3; }
M=aws/anthropic/bedrock-claude-opus-4-8
cget() { HERMES_HOME="$GW" hermes "$@"; }
```

`cget` runs `hermes` under the gateway home; `cget -p triager …` reaches the triager profile.

1. **Lane pre-flight** (`/workspace/shared/wiki/concepts/hermes-runtime-openshell-lane-preflight-and-stacked-rows.md`):
   the image check the LANE READY names (`.fork-sha` and the web_dist checksums), the image's fork
   mirror holds the PR head (`git -C /opt/hermes/fork cat-file -e $SHA^{commit}; echo rc=$?` →
   `rc=0`), and at most 17 live sandboxes exist. Record each in `$EV`. Any miss is `FAIL(env)`.
2. **Lane assertions for the §D6 install-scan guard** (`website/docs/user-guide/fleet-openshell.md`
   § install-scan guard). The guard is sound only inside this boundary, so both hold or nothing is
   installed:
   - `git -C /opt/hermes/fork rev-parse HEAD` → expect: `$SHA`.
   - `$HPY -c 'import socket;s=socket.create_connection(("10.200.0.1",3128),10);s.sendall(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n");print(s.recv(64).split(b"\r\n")[0].decode())'`
     → expect: a non-`200` status line (GitHub egress refused).

   Either miss is `FAIL(env)`.
3. **Managed scope and guard.** Resolve the scope with the release resolver itself
   (`hermes_cli/managed_scope.py:52-71`; it does not follow `HERMES_HOME`). When
   `HERMES_MANAGED_DIR` is set and non-empty, it is the scope if it is a directory, and if it is
   not a directory there is no scope (no fallback). Only when it is unset or empty is `/etc/hermes`
   used, if it is a directory:

   ```bash
   MD=$("$HPY" -c 'from hermes_cli.managed_scope import get_managed_dir; d=get_managed_dir(); print(d if d is not None else "")') \
     || { echo "FAIL(env): managed scope unresolvable" >> "$EV"; exit 3; }
   echo "managed_scope=${MD:-none}" >> "$EV"
   [ -n "$MD" ] && [ -w "$MD" ] && { [ ! -e "$MD/config.yaml" ] || [ -w "$MD/config.yaml" ]; } \
     || { echo "FAIL(env): managed scope ${MD:-none} absent or unwritable" >> "$EV"; exit 3; }
   ```

   No scope, or an unwritable one, is `FAIL(env)` with nothing written. Record the scope's
   `plugins.scan_on_install` before the write
   (`echo "scan_before=$(cget config get plugins.scan_on_install 2>&1)" >> "$EV"`), then merge the
   documented guard into `$MD/config.yaml`, keeping its other keys:
   `$HPY -c 'import sys,yaml,pathlib;p=pathlib.Path(sys.argv[1])/"config.yaml";c=(yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {};c.setdefault("plugins",{})["scan_on_install"]=False;p.write_text(yaml.safe_dump(c),encoding="utf-8")' "$MD"`
   → expect: exit 0. Then `cget config get plugins.scan_on_install` → expect: `false` (under the
   gateway home). The triager profile does not exist yet; its check is in step 5.
4. **Plugins at the head, the documented path only.** First assert that the lane's git rewrite maps
   the fork's GitHub URL (the URL the `owner/repo/...` shorthand resolves to,
   `hermes_cli/plugins_cmd.py:239-241`) onto the mirror:


   ```bash
   git config --get-regexp '^url\..*\.insteadof$' >/dev/null \
     || { echo "FAIL(env): no url.*.insteadof rewrite" >> "$EV"; exit 3; }
   u=$(git ls-remote --get-url https://github.com/slang-coworkers/hermes-agent.git)
   case "$u" in
     /opt/hermes/fork|file:///opt/hermes/fork) echo "rewrite=$u" >> "$EV" ;;
     *) echo "FAIL(env): fork URL does not resolve to the mirror" >> "$EV"; exit 3 ;;
   esac
   ```

   Only the accepted mirror path is recorded: a rewrite key can embed a credential, so neither the
   rewrite list nor an unexpected URL is ever written out. With GitHub egress
   refused (step 2), an install that succeeds can only have read the mirror. Then, for each of
   `nv-coworker-compose` and `nv-fleet-gates`, in the gateway home now and in the triager home after
   step 5's profile install, with this function:

   ```bash
   ensure_plugins() {  # $1 = home dir; remaining args = hermes profile flags (none, or -p triager)
     local H=$1 name rev on rc; shift
     for name in nv-coworker-compose nv-fleet-gates; do
       rev=$("$HPY" -c 'import json,os,sys; p=sys.argv[1]+"/plugins/.install-metadata.json"; m=json.load(open(p,encoding="utf-8")) if os.path.exists(p) else {}; print(m.get(sys.argv[2],{}).get("revision","absent"))' "$H" "$name") \
         || { echo "FAIL(env): $H install metadata unreadable" >> "$EV"; return 3; }
       if [ "$rev" = absent ]; then
         cget "$@" plugins install "slang-coworkers/hermes-agent/plugins/$name" --ref "$SHA" --enable; rc=$?
         echo "$H $name absent install rc=$rc" >> "$EV"
       elif [ "$rev" = "$SHA" ]; then
         on=$(set -o pipefail; cget "$@" config get plugins.enabled --json | "$HPY" -c 'import json,sys;print(sys.argv[1] in (json.load(sys.stdin) or []))' "$name") || on=
         case "$on" in
           True)  rc=0; echo "$H $name $rev skip rc=0" >> "$EV" ;;
           False) cget "$@" plugins enable "$name"; rc=$?; echo "$H $name $rev enable rc=$rc" >> "$EV" ;;
           *)     rc=3; echo "FAIL(env): $H $name enabled state unreadable" >> "$EV" ;;
         esac
       else rc=3; echo "FAIL(env): $H $name at $rev, not $SHA" >> "$EV"; fi
       [ "$rc" = 0 ] || return 3
     done
   }
   ensure_plugins "$GW" || exit 3
   ```

   The branches: absent → `plugins install … --ref $SHA --enable` (the installer's own source form,
   `plugins/nv-coworker-compose/openshell/installer.py:434-445`); at `$SHA` and enabled → skip; at
   `$SHA` but disabled → `plugins enable`; any other revision → `FAIL(env)`, never `--force`. Step 5
   runs `ensure_plugins "$GW/profiles/triager" -p triager` for the triager home.
   Each plugin leaves one `<home> <name> <rev> <action> rc=<exit>` line in `$EV`. There is no tree
   copy and no `--force` of any kind. After all four, record the scanner config again
   (`scan_after_gw=…`, `scan_after_triager=…` from `cget [-p triager] config get plugins.scan_on_install`).
5. **Render, profile and trust.** `cget coworker compose $SPEC --out /tmp/e13-render` then
   `cget profile install /tmp/e13-render/triager --name triager -y` → expect: exit 0. Right after
   the profile install, and before the triager-home plugin installs of step 4, assert that
   `cget -p triager config get plugins.scan_on_install` reads `false` (the same managed scope). Then
   `ensure_plugins "$GW/profiles/triager" -p triager || exit 3` (step 4's function). Then `cget coworker install-trust $SPEC` → expect: exit 0 and a JSON
   line with `"ok": true`. Both homes carry the block:
   `grep -c '^  openshell_trust:' $GW/config.yaml $GW/profiles/triager/config.yaml` → expect: `1` for each.
6. **Scenario-local triager route.** The render gives the triager a provider on the external host
   (`tests/e2e-scenarios/FLEET-F62.c/fixtures/openshell/triager/config.yaml:1-9`). Point it at the
   route with the documented `hermes config set <key> <value>` surface, for the rendered provider
   `coworkers-live` (`$M` = the front-matter `model_id`):

   ```bash
   cget -p triager config set providers.coworkers-live.base_url https://inference.local/v1
   cget -p triager config set providers.coworkers-live.api_mode chat_completions
   cget -p triager config set providers.coworkers-live.default_model "$M"
   cget -p triager config set model.default "$M"
   ```

   → expect: exit 0 for each. Then assert the effective route before turn 1 with one keyed
   `cget -p triager config get <key>` per key (it resolves the merged config,
   `hermes_cli/config.py:6001-6006`): `model.provider` = `coworkers-live`, plus each of the four
   keys above = its set value. Also assert the gateway policy in force allows the route: the step-2
   CONNECT one-liner with `inference.local:443` → expect: `HTTP/1.1 200`.
   Save the probe and the session before the step-7 freeze. The probe runs plugin discovery, whose
   secret-source refresh calls `load_hermes_dotenv()`, and prints only the effective `SSL_CERT_FILE`
   path and a bridge flag, never an env value. The restart in step 2 ends the sandbox shell, so the
   session file carries what the later steps need; the first command in any new shell is
   `. /tmp/e13-session.sh`:

   ```bash
   probe() {
     HERMES_HOME="$1" "$HPY" -c 'import os; from hermes_cli.plugins import discover_plugins; discover_plugins(); np = os.environ.get("NO_PROXY", "") + "," + os.environ.get("no_proxy", ""); print(os.environ.get("SSL_CERT_FILE", "")); print("bridge" if "172.17.0.1" in np or "host.docker.internal" in np.lower() else "no-bridge")'
   }
   { printf 'GW=%q\nHPY=%q\nEV=%q\nM=%q\nSHA=%q\n' "$GW" "$HPY" "$EV" "$M" "$SHA"; declare -f cget probe; } > /tmp/e13-session.sh
   ```

7. Record the triager config digest (taken after step 6) and `H1`:
   `sha256sum $GW/profiles/triager/config.yaml > /tmp/e13-cfg.sha`,
   `sha256sum /etc/openshell-tls/ca-bundle.pem | cut -c1-64 > /tmp/e13-H1`, then
   `cp /etc/openshell-tls/ca-bundle.pem /tmp/e13-stale-ca.pem` (the stale-copy control; it is
   never referenced by any Hermes config). No file is edited after this step until teardown.

## Steps

1. Run one model turn as the triager on the route: `timeout 300 env HERMES_HOME=$GW hermes -p triager chat -q "reply with the word ok"`.
   Record the model id the response reports. If the route serves a different model than
   `model_id`, record the served id; that does not fail the run.
   Then run `probe $GW/profiles/triager` and `probe $GW` → expect: the turn exits 0 with a reply,
   and both probes print `/etc/openshell-tls/ca-bundle.pem` then `no-bridge`.
2. **Restart the sandbox:** `openshell sandbox restart fleet-f62d-e13gw` → expect: exit 0 and the
   sandbox Ready again. In the new sandbox shell, `. /tmp/e13-session.sh && type probe` → expect:
   success. Then `sha256sum -c /tmp/e13-cfg.sha` → expect: OK (the triager `config.yaml` is unchanged).
3. Record `H2`: `sha256sum /etc/openshell-tls/ca-bundle.pem | cut -c1-64` → expect: `H2 != $(cat /tmp/e13-H1)`
   (a re-mint was observed). If `H2 == H1`, the premise was not exercised: report `ESCALATE`,
   never PASS.
4. With no file edited, repeat step 1's model turn and both probes → expect: exit 0 with a reply,
   and both probes again print the live path and `no-bridge`.
5. Stale-copy control, through the proxy tunnel. First record the stale copy's digest:
   `sha256sum /tmp/e13-stale-ca.pem | cut -c1-64` → expect: it equals `H1` (the copy is the
   pre-restart bundle). If it differs, the control does not test a stale copy: report `ESCALATE`,
   never PASS. Then run both verify attempts through one function, so only the CA file differs:

   ```bash
   "$HPY" - <<'PY'
   import socket, ssl, sys
   PROXY, TARGET, SNI = ("10.200.0.1", 3128), "inference.local:443", "inference.local"
   def tunnel():
       s = socket.create_connection(PROXY, timeout=30)
       s.sendall(f"CONNECT {TARGET} HTTP/1.1\r\nHost: {TARGET}\r\n\r\n".encode())
       head = b""
       while b"\r\n\r\n" not in head:
           chunk = s.recv(1)
           if not chunk:
               sys.exit("FAIL(env): proxy closed before the CONNECT response")
           head += chunk
       status = head.split(b"\r\n", 1)[0].decode()
       print("proxy:", status)
       if status.split()[1:2] != ["200"]:
           sys.exit("FAIL(env): CONNECT " + status)
       return s
   def verify(label, cafile):
       ctx = ssl.create_default_context(cafile=cafile)
       try:
           ctx.wrap_socket(tunnel(), server_hostname=SNI).close()
           print(label, "handshake-ok")
           return True
       except ssl.SSLCertVerificationError as exc:
           print(label, type(exc).__name__, "reason:", exc.verify_message or exc.reason)
           return False
   print(f"settings: proxy={PROXY[0]}:{PROXY[1]} target={TARGET} sni={SNI} "
         f"client=ssl.create_default_context(cafile=<varies>) timeout=30 (identical for both attempts)")
   stale_ok = verify("stale", "/tmp/e13-stale-ca.pem")
   live_ok = verify("live", "/etc/openshell-tls/ca-bundle.pem")
   sys.exit(0 if (not stale_ok and live_ok) else 1)
   PY
   ```

   → expect: exit 0, the `settings:` line once, both proxy status lines `200` (otherwise the script
   exits with `FAIL(env)`), `stale` prints `SSLCertVerificationError` with its reason string, and
   `live` prints `handshake-ok`. Exit 1 means the stale copy verified or the live bundle did not. The positive control proves that the tunnel, not a direct dial, was exercised.
6. Teardown (always, also after a failure): delete `fleet-f62d-e13gw` → expect: not listed by
   `openshell sandbox list`.

## Pass

The criterion holds when both model turns succeed across an observed re-mint (`H1 != H2`) with no
file edited, both homes' probes name the live bundle and no bridge entry after the restart, and
through the same proxy tunnel the stale copy fails verification while the live bundle verifies.

## Evidence

- `scenario-AC-FLEET-F62.e-13/evidence.txt`: the gateway policy's `sha256`, the image check, the two lane assertions (mirror HEAD,
  the GitHub CONNECT status line) and the `insteadof` rewrite, the resolved managed scope and the
  scanner config before and after, each plugin's `<home> <name> <rev> <action> rc=<exit>` line, the
  effective triager route (the five `config get` values) and the `inference.local:443` policy
  check, `H1`, `H2`, each step's exit code, the served model id of each turn, each probe's two printed lines, and for the step-5
  control: the `sha256` of `/tmp/e13-stale-ca.pem` (must equal `H1`), the `settings:` line (same
  proxy, target `inference.local:443`, SNI `inference.local`, tunnel and client settings for both
  attempts; only the CA file differs), the two proxy status lines, and the two result lines (the
  stale attempt's error class and its reason string). No env value other than the probe's path.
  This is a shell-only live outcome (P5): no screenshot.
- `scenario-AC-FLEET-F62.e-13/openshell.log`: the restart, and the allow lines for the two
  inference calls.
- Model-call count for the bound, from the triager home:
  `GW="$GW" python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['GW'] + '/profiles/triager/state.db'); print(*d.execute('select coalesce(sum(api_call_count),0) from session_model_usage').fetchone())"`
