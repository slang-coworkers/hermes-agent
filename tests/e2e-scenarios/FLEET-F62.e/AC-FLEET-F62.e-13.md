---
ac: AC-FLEET-F62.e-13
kind: live
model: live
base_url: https://inference-api.nvidia.com
api_mode: anthropic_messages
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

`model: live`: the harness supplies its non-secret sentinel for the provider key field, and the
OneCLI live tier injects the real credential at the egress boundary. At most 4 model calls (two
turns, one retry headroom each). This is not agent-to-agent: one profile, `triager`.

## Gating

- **Restart verb.** Step 2 runs `openshell sandbox restart fleet-f62d-e13gw` (lane verb, allowed for
  `fleet-f62d-*` only). It is a docker restart of that one sandbox container: it keeps the
  filesystem and the home, re-mints the CA, and returns once the restart completes.
  Delete-and-recreate is not a restart. The scenario still requires an observed re-mint (step 3).
- **Lane pre-flight first** (`/workspace/shared/wiki/concepts/hermes-runtime-openshell-lane-preflight-and-stacked-rows.md`):
  the image's fork mirror holds the PR head (`git -C /opt/hermes/fork cat-file -e <sha>^{commit}`
  rc 0, probed in a throwaway sandbox that is deleted on exit) and at most 17 live sandboxes exist
  (this scenario adds 1 to the shared cap of 18). Any miss is `FAIL(env)`, uncounted.

## Fixtures

None installed by the harness (`fixtures: []`). The `triager` distribution comes from the
PR-head render of `tests/e2e-scenarios/FLEET-F62.c/spec/openshell/` (the fleet spec of record),
which carries `egress.openshell_trust.bundle: /etc/openshell-tls/ca-bundle.pem`.

## Setup

Everything below runs inside the one sandbox, `fleet-f62d-e13gw`, created from the lane gateway
image `osh-f64-gateway:pinned` with the gateway policy that allows the inference route. `$SHA` is
the full 40-character PR head; `$SPEC` is the in-image checkout of
`tests/e2e-scenarios/FLEET-F62.c/spec/openshell/coworker-types.yaml` at `$SHA`; `$GW` is the
sandbox's gateway home (`$HERMES_HOME` of the gateway process).

1. Resolve the Hermes interpreter once:
   `for p in /opt/hermes/.venv/bin/python /opt/hermes/venv/bin/python python3; do "$p" -c 'import hermes_cli' 2>/dev/null && HPY=$p && break; done; echo "HPY=$HPY"`
   → expect: a non-empty `HPY`.
2. Install `nv-coworker-compose` (and `nv-fleet-gates`) at the PR head into `$GW`:
   `HERMES_HOME=$GW hermes plugins install slang-coworkers/hermes-agent/plugins/nv-coworker-compose --ref $SHA --enable`
   (the same for `nv-fleet-gates`) → expect: exit 0 for each.
3. Render and install the triager:
   `HERMES_HOME=$GW hermes coworker compose $SPEC --out /tmp/e13-render` then
   `HERMES_HOME=$GW hermes profile install /tmp/e13-render/triager --name triager -y`, and the two
   plugins into the triager home (`hermes -p triager plugins install … --ref $SHA --enable`)
   → expect: exit 0 for each.
4. Carry the trust block into the gateway home: `HERMES_HOME=$GW hermes coworker install-trust $SPEC`
   → expect: exit 0 and a JSON line with `"ok": true`.
5. Both homes carry the block:
   `grep -c '^  openshell_trust:' $GW/config.yaml $GW/profiles/triager/config.yaml` → expect: `1` for each.
6. Record the pre-restart CA and the stale-copy control:
   `sha256sum /etc/openshell-tls/ca-bundle.pem | cut -c1-64 > /tmp/e13-H1` and
   `cp /etc/openshell-tls/ca-bundle.pem /tmp/e13-stale-ca.pem`. The stale copy is never
   referenced by any Hermes config. Also record the triager config digest:
   `sha256sum $GW/profiles/triager/config.yaml > /tmp/e13-cfg.sha`.

The probe used in steps 1 and 4 (prints only the effective `SSL_CERT_FILE` path and a bridge flag,
never an env value):

```bash
probe() {  # $1 = HERMES_HOME to start under
  HERMES_HOME="$1" "$HPY" -c 'import os
from hermes_cli.plugins import discover_plugins
discover_plugins()
np = os.environ.get("NO_PROXY", "") + "," + os.environ.get("no_proxy", "")
print(os.environ.get("SSL_CERT_FILE", ""))
print("bridge" if "172.17.0.1" in np or "host.docker.internal" in np.lower() else "no-bridge")'
}
```

## Steps

1. Run one model turn as the triager: `timeout 300 hermes -p triager chat -q "reply with the word ok"`.
   Then run `probe $GW/profiles/triager` and `probe $GW` → expect: the turn exits 0 with a reply,
   and both probes print `/etc/openshell-tls/ca-bundle.pem` then `no-bridge`.
2. **Restart the sandbox:** `openshell sandbox restart fleet-f62d-e13gw` → expect: exit 0, the
   sandbox is Ready again, and `sha256sum -c /tmp/e13-cfg.sha` passes (the triager `config.yaml`
   is unchanged).
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
   import socket, ssl
   PROXY, TARGET, SNI = ("10.200.0.1", 3128), "inference-api.nvidia.com:443", "inference-api.nvidia.com"
   def tunnel():
       s = socket.create_connection(PROXY, timeout=30)
       s.sendall(f"CONNECT {TARGET} HTTP/1.1\r\nHost: {TARGET}\r\n\r\n".encode())
       head = b""
       while b"\r\n\r\n" not in head:
           head += s.recv(1)
       print("proxy:", head.split(b"\r\n", 1)[0].decode())
       return s
   def verify(label, cafile):
       ctx = ssl.create_default_context(cafile=cafile)
       try:
           ctx.wrap_socket(tunnel(), server_hostname=SNI).close()
           print(label, "handshake-ok")
       except ssl.SSLCertVerificationError as exc:
           print(label, type(exc).__name__, "reason:", exc.verify_message or exc.reason)
   print(f"settings: proxy={PROXY[0]}:{PROXY[1]} target={TARGET} sni={SNI} "
         f"client=ssl.create_default_context(cafile=<varies>) timeout=30 (identical for both attempts)")
   verify("stale", "/tmp/e13-stale-ca.pem")
   verify("live", "/etc/openshell-tls/ca-bundle.pem")
   PY
   ```

   → expect: the `settings:` line once, both proxy status lines are `200` (otherwise the control is
   `FAIL(env)`), `stale` prints `SSLCertVerificationError` with its reason string, and `live` prints
   `handshake-ok`. The positive control proves that the tunnel, not a direct dial, was exercised.
6. Teardown (always, also after a failure): delete `fleet-f62d-e13gw` → expect: not listed by
   `openshell sandbox list`.

## Pass

The criterion holds when both model turns succeed across an observed re-mint (`H1 != H2`) with no
file edited, both homes' probes name the live bundle and no bridge entry after the restart, and
through the same proxy tunnel the stale copy fails verification while the live bundle verifies.

## Evidence

- `scenario-AC-FLEET-F62.e-13/evidence.txt`: `H1`, `H2`, each step's exit code, each probe's two
  printed lines, and for the step-5 control: the `sha256` of `/tmp/e13-stale-ca.pem` (must equal
  `H1`), the `settings:` line (same proxy, target host:port, SNI, tunnel and client settings for
  both attempts; only the CA file differs), the two proxy status lines, and the two result lines
  (the stale attempt's error class and its reason string). No env value other than the probe's
  path. This is a shell-only live outcome (P5): no screenshot.
- `scenario-AC-FLEET-F62.e-13/openshell.log`: the restart, and the allow lines for the two
  inference calls.
- Model-call count for the bound, from the triager home:
  `python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['GW'] + '/profiles/triager/state.db'); print(*d.execute('select coalesce(sum(api_call_count),0) from session_model_usage').fetchone())"`
