---
ac: AC-ISO-F14-2
kind: sandbox
model: n/a
fixtures:
  - fixtures/iso-f14-worker
timeout_s: 300
---

# AC-ISO-F14-2 — a direct (non-proxy) request from the sandbox is refused at connect

From the SAME sandbox as AC-ISO-F14-1, a direct (non-proxy) request to the same API
host is refused at connect — `curl --noproxy '*'` exits non-zero with http_code `000`
— proving the sandbox has no second route out. This is the narrow single-host
direct-connect check; the comprehensive network-level exclusivity proof ("no packet can
leave except via 172.17.0.1:10255" across all destinations/protocols) stays deferred to
P7/APF (ruling pt 4/6).

## Setup
- The AC-ISO-F14-1 container is up — **reuse it, do not spawn a second.**
- **Required testbed prerequisite (ADDENDUM item 9, operator deployment — NOT rendered
  by ISO-F14):** a host egress rule for uid `hermes-sandbox` permitting only
  `172.17.0.1:10255`, and the S6 pre-flight's direct-connect check (item 5) has
  confirmed it (a raw connect to a non-proxy host from the sandbox uid is blocked).
  **If S6 reports this rule absent, this criterion is BLOCKED (infra), NOT a FAIL.**

## Steps
1. From inside the sandbox, bypassing the proxy env, run
   `curl -s -o /dev/null -w '%{http_code}' --noproxy '*' https://<the same API host as AC-1>`.
   → expect: a connect failure — non-zero curl exit and http_code `000`.

## Pass
The direct (non-proxy) request is refused at connect (`000`, non-zero curl exit). A
`200` is a FAIL (a second route exists). A `401`/`403` is ALSO a FAIL (the connection
succeeded — the egress rule is not enforcing).

## Evidence
- `scenario-AC-ISO-F14-2/direct-connect.log` — the curl exit code + http_code of the
  `--noproxy '*'` attempt.
