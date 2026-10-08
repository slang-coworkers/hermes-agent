# iso-f14-worker

ISO-F14 sandbox worker.

## Context
- Fixture profile for the ISO-F14 egress-lockdown sandbox scenarios (AC-ISO-F14-1/2/5).
- Carries the OPTION-A egress posture rendered by nv-coworker-compose; a sandbox spawned off it reaches egress only through the OneCLI proxy and holds no raw credential.

## Invariants
- Egress leaves only through the OneCLI proxy (http://172.17.0.1:10255); a direct route is refused.
- No raw credential in the sandbox env; OneCLI injects at request time.
