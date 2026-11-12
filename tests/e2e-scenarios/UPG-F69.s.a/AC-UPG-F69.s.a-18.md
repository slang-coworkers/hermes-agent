---
ac: AC-UPG-F69.s.a-18
kind: live
model: none
fixtures: []
LIVE_MODEL_CALLS_MAX: 0
timeout_s: 900
---

# AC-UPG-F69.s.a-18 — a scratch-prefix re-install of the head on v0225 comes up working with no manual box step (live)

**Criterion.** One scratch-prefix re-install of the head on brev-hermes-v0225 comes up working with no manual box
step. On both runs, install 1 and the immediate re-install 2:

- `install_rc=0`;
- the routed script is byte-identical in the orchestrator profile, and the v0.22.5 resolver run in that profile's
  home returns it;
- 10 directed edges sit in a store under the scratch home;
- `ssh` to all five aliases exits 0 through the durable shim.

Run 2 adds zero sandbox ops. The sealed `/opt/hermes/fork` stays unchanged, and S0/S4 shows the shipped fleet
untouched. (Items 1, 3–7.)

**Kind note.** The install makes no model call: `model: none`, `LIVE_MODEL_CALLS_MAX: 0`. `timeout_s` bounds one
install poll: each install is polled for at most 15 min and never launched twice. The evidence is `evidence.txt`
plus logs; no screenshots.

**Template.** The AC-FLEET-F62.f-9 sheet at Y (sha256 40bb9a2f, Orchestrator msgs 8/10): its S0, S1.2, S1.5, Step 1,
Step 3 and S4 are referenced below by those names, and they are carried as that sheet states them.

## Standing rules (every hand-off)

- A timing-only failure with a same-node base match in an untouched file is ADVISORY (msg 3088).
- A live miss on model wording, format or timing is a WATCH item, never a counted FAIL (msg 3730).
- Any delete for disk space is asked first. One row in test on the box at a time.
- CI at the head: every lane green, except exact node ids in untouched files that also fail in fork CI at base
  (DN-24 test).
- The gate ends at MERGE-READY; bots never ready or merge.
- **Ids (A6):** the Results row and the reviewer's cross-walk spell `AC-UPG-F69.s.a-18` verbatim, dots included.
- **Item-2 precondition (orch msg 20):** item 2 is DONE. fleet-host-watchdog.sh keeps the 127.0.0.1:28645 →
  v0225:8644 forward alive, and the msg-16 handshake is withdrawn. As a precondition, record team-lead's read that the
  forward is listening in `evidence.txt`. If it is not listening, that is FAIL(env) and the run stops.

## Box steps are test orchestration only

- Team-lead runs every step, one at a time; hermes-tester never execs on v0225.
- A step needed to make the install work, after the launch, counts as a manual step and FAILs this criterion.
- Exec form: `openshell sandbox exec -g nemoclaw -n brev-hermes-v0225 --no-tty -- sh -lc '<one line>'`. An argv
  element never holds a newline.

## Values

- `GW` = brev-hermes-v0225. `SHIP_HH=/sandbox/.hermes`, `SHIP_MD=/sandbox/.hermes/managed` (read only, in S0/S4).
- `PFX=fleet-f62d-s5a`, inside the broker's `fleet-f62d-` allow-list (team-lead prerequisite for `policy digest`,
  `sandbox create … --policy-sha256` and `sandbox delete`).
- `SCR_HOME=/sandbox/scratch/fleet-f62d-s5a-home`, `SCR_MD=/sandbox/scratch/fleet-f62d-s5a-managed`,
  `SCR_USER=/sandbox/scratch/fleet-f62d-s5a-user`; all NEW, must not exist.
- `$HEAD` = the PR head's full 40-hex sha, never the word `HEAD`. If the PR head moves, stop.
- `SENV` = `export HERMES_HOME=<SCR_HOME> HERMES_MANAGED_DIR=<SCR_MD> HOME=<SCR_USER>`. It is exported AFTER
  `. /sandbox/.hermes/.gateway-env` (the Y launch env), so the GitHub fetch has GW's egress env. Any `HERMES_*` that
  file sets is overridden. Record `env | grep -c '^HERMES_HOME=/sandbox/scratch/'` = 1.
- No `PATH` edit and no `lane.env` sourcing at launch: item 4 requires the installer to reach openshell through the
  spec's `ssh_lane` itself.

## Why a scratch PREFIX alone does not isolate the shipped fleet

A scratch `project:` renames only the worker sandboxes (`<project>-<role>`). Everything else the installer writes
lands in whatever `HERMES_HOME`, `HERMES_MANAGED_DIR` and `HOME` it runs under:

- the profiles, installed under the bare role names;
- the managed `nv-fleet-gates` maps (`expected_ssh_host` is replaced whole, so it would re-point the shipped veto at
  the scratch sandboxes);
- the parked non-fleet profiles and the in-place default config edit;
- the rooms, created over the `--gateway-url` WebSocket, and the gateway restart;
- the `~/.ssh/config` Include line and the per-project include;
- the edges store: an absolute `edges_db_path` is shared by every home that names it, which is why the specs now
  name a root-relative one.

So every launch below runs under `SENV` (a fresh home, managed dir and user home) against the scratch gateway's
`SCR_URL`. S0 and S4 prove the shipped fleet was not touched.

## Protocol vs manual step (A3; D-3 option 1, team-lead msg 100)

- These are test orchestration, listed step by step below:
  - phase-a at `$HEAD`;
  - the L1/L2 render with its digest checks;
  - the operator-gated host policy mirror under `--policy-root` (the policy root stays operator-only);
  - the Setup items: S0, the disk gate, L3, staging the head, SCR_SPEC, the scratch gateway start, and seeding the
    ship's PRE-install state. That state is the root routes and the root `scripts/github_to_botchat.py`, which the
    ship already holds (B6).
- Lane-bundle delivery into a re-created GW stays with host tooling (`f2s-lane-go.sh`). This run uses the existing
  GW, where the bundle is present.
- **Any ship-gap fix FAILS this criterion, before or after a launch.** That means a route-script copy into a
  profile, a `wire add`, a lane re-apply or ProxyCommand edit, a writable fork copy or git wrapper, or a bundle
  upload.

## Setup (before the launch; none of it repairs the install)

- **S0** exactly as the F62.f-9 sheet. Additionally record `sha256sum /tmp/hermes-nanoclaw-base/edges.db`.
  - **A7 baseline, before phase-a:** `touch /sandbox/scratch/fleet-f62d-s5a-s0.marker`, then record three values for
    `/opt/hermes/fork`:
    - `git -C /opt/hermes/fork for-each-ref --format='%(refname) %(objectname)' | sort | sha256sum`;
    - `git -C /opt/hermes/fork rev-parse HEAD`;
    - `git -C /opt/hermes/fork status --porcelain | sha256sum` (the content, not a count).
  - If git refuses with "dubious ownership", read with `git -c safe.directory=/opt/hermes/fork …`. Never write a
    config.
- **Disk gate** (the F62.f-9 sheet's `df -B1 -P /` line: ≥ 21.4e9 B free and ≤ 92 %), run immediately before EACH
  launch (phase-a, install 1, install 2). `evidence.txt` records a `GATE-OK` line before each; `HOLD` → stop and
  report. Pre-launch bound: the run needs ≤ 2 GB (5 sandboxes plus a ~1 MB fork delta).
- **L3:** `openshell sandbox list | sed 's/\x1b\[[0-9;]*m//g' | grep -c '^fleet-f62d-s5a-'` = 0 (SGR stripped first,
  item 5).
- **Stage the head** in GW under `/sandbox/scratch/fleet-f62d-s5a-src/` (the act that puts "the installer at the
  row's head" into GW):
  - a scratch bare repo, and a `git fetch` of `$HEAD` from `https://github.com/slang-coworkers/hermes-agent.git`;
  - `git archive $HEAD plugins tests/e2e-scenarios/FLEET-F62.c/spec | tar -x`.
  - It must NOT create `<SCR_HOME>/openshell/`. Record that the dir is absent.
- **Interpreter (P0, erratum E2 Q2).** Under `sh -lc` + `.gateway-env`, record in `evidence.txt` the box facts
  `command -v python3`, `python3 -c 'import hermes_yaml'` and `/opt/hermes/.venv/bin/python3 -c 'import hermes_yaml'`
  (exit code each). There is no `PATH` edit and no exported variable either way. `<PY>` and `<PYENV>` below are then:
  - the bare `python3` imports `hermes_yaml`: `<PY>` = `python3`, `<PYENV>` = empty (the commands run as written);
  - else `/opt/hermes/.venv/bin/python3` does: `<PY>` = `/opt/hermes/.venv/bin/python3`, named per command, and
    `<PYENV>` = `HERMES_PYTHON=/opt/hermes/.venv/bin/python3 `, scoped to the one launch command, which
    `install-into-sandbox.sh`'s `pick_python` honours first;
  - neither does: HOLD, FAIL(env), and the box read goes to the Orchestrator.
- **SCR_SPEC.**
  - `SCR_SPEC_DIR` = the staged head's `tests/e2e-scenarios/FLEET-F62.c/spec/openshell/` dir, copied to
    `/sandbox/scratch/fleet-f62d-s5a-spec/`.
  - `SCR_SPEC=$SCR_SPEC_DIR/coworker-types.yaml` (a FILE; `--spec` takes the file).
  - Its `egress` is replaced by the shipped spec's (`/sandbox/scratch/e2e-spec-run3/coworker-types.yaml`) `egress`,
    and `project` is set to `fleet-f62d-s5a`, with the F62.f-9 S1.2 yaml one-liner.
  - D-4: the same one-liner also sets `types.reviewer.providers` to `[]`, so the scratch reviewer gets no GitHub
    credential.
  - Its `ssh_lane` (bundle `/tmp/osh-lane`, read only) and its sibling `wire-manifest.txt` come from the head
    unchanged.
  - Check: `test -f "$SCR_SPEC" && <PY> <src>/plugins/nv-coworker-compose/openshell/installer.py plan --spec
    "$SCR_SPEC" --ref "$HEAD" --policy-root <ROOT> --fork-source https://github.com/slang-coworkers/hermes-agent.git`
    exits 0, prints five `fleet-f62d-s5a-*` creates and five `hermes wire add` lines, and names no `<SHIP_HH>`,
    `<SHIP_MD>` or `nanoclaw-base-*` (F62.f-9 S1.6). `plan` is read-only: it reports mirror work (a
    `# fork mirror:` line) but fetches and writes nothing.
- **Seed the scratch home with the ship's pre-install hand config (B6).** This reproduces the state a re-install
  starts from:
  - `<SCR_HOME>/config.yaml` = `{platforms: {webhook: <SHIP_HH webhook block with extra.port → a free port>}}`;
  - `cp -p <SHIP_HH>/scripts/github_to_botchat.py <SCR_HOME>/scripts/`.
  - No secret is printed.
- **Scratch gateway:** started under `SENV` on free loopback ports, the same way the shipped gateway and serve were
  started (F62.f-9 S1.5), giving `SCR_URL`.

## Steps

1. Install 1, in the F62.f-9 order:
   - Phase A at `$HEAD`: `<SENV>; <PY> <src>/plugins/nv-coworker-compose/openshell/installer.py phase-a --spec
     "$SCR_SPEC" --ref "$HEAD" --fork-source https://github.com/slang-coworkers/hermes-agent.git`, expecting
     `phasea_rc=0`;
   - L1/L2 for `PFX` (render, host mirrors under `ROOT`, digests equal);
   - the launch, `<SENV>; <PYENV>nohup bash <src>/plugins/nv-coworker-compose/openshell/install-into-sandbox.sh
     "$SCR_SPEC" --ref "$HEAD" --gateway-url <SCR_URL> --policy-root <ROOT> --fork-source <same url>
     > /tmp/f69sa-install-1.log 2>&1; echo install_rc=$? >> /tmp/f69sa-install-1.log &`, polled as in F62.f-9 Step 1.
   → expect: `install_rc=0`; five `fleet-f62d-s5a-*` Ready, each create carrying `--policy-sha256`.
2. Item 6 → expect:
   - the three A7 values (for-each-ref digest, HEAD, porcelain sha256) equal S0;
   - `find /opt/hermes/fork -newer /sandbox/scratch/fleet-f62d-s5a-s0.marker -print -quit` prints nothing;
   - the installer's install metadata records `$HEAD` for every nv-* plugin in `<SCR_HOME>` and in each profile
     (`<home>/plugins/.install-metadata.json`, `revision`);
   - `<SCR_HOME>/openshell/` holds the installer's mirror (`fork-mirror.git`) and git wrapper (`gitbin/git`).
3. Item 1 → expect:
   - `sha256sum <SCR_HOME>/scripts/github_to_botchat.py <SCR_HOME>/profiles/orchestrator/scripts/github_to_botchat.py`
     gives two equal digests;
   - `HERMES_HOME=<SCR_HOME>/profiles/orchestrator python3 -c 'from gateway.platforms.webhook_filters import
     _resolve_script_path as r; p,e=r("github_to_botchat.py"); print(p, e)'` prints that path and `None`.
4. Item 3 → expect:
   - `<SENV>; hermes -p orchestrator wire list` shows 10 directed edges (the 5 manifest pairs both ways, none gated);
   - the store file named by the resolved `edges_db_path` (`<SCR_HOME>/fleet/edges.db`) is under `<SCR_HOME>`;
   - `/tmp/hermes-nanoclaw-base/edges.db` sha256 equals S0.
5. Items 4/7 → expect:
   - each of the 5 `Host openshell-fleet-f62d-s5a-<role>` blocks in
     `<SCR_USER>/.ssh/config.d/openshell-fleet-f62d-s5a.conf` carries the lane ProxyCommand (`/tmp/osh-lane/…`,
     `--name fleet-f62d-s5a-<role>`);
   - `ssh -F <SCR_USER>/.ssh/config openshell-fleet-f62d-s5a-<role> uname -n; echo rc=$?` gives `rc=0` for all 5,
     using the key path from that profile's `terminal.ssh_key`, as f2s-relane's proof did.
6. Install 2 (the re-install): the same launch with log `/tmp/f69sa-install-2.log`.
   → expect:
   - `install_rc=0`;
   - the T2..T2e broker audit has zero `sandbox-create`, `-replace` or `-delete` (item 5 under colour);
   - the ssh include's sha256 equals its value after step 5 (item 7);
   - step 5's ssh probes give `rc=0` ×5 again;
   - steps 3 and 4 read the same.
7. **S4** as F62.f-9, plus the S0 extras: the edges.db sha, the three A7 fork values, and an empty S0-marker `find`.
   → expect: every value identical, the shipped gateway/serve pids and start times identical, and the host list
   equal to S0's. S4 is repeated as **S4′ after AC-UPG-F69.s.a-19** with the same expectations.
8. Teardown: AFTER AC-UPG-F69.s.a-19, as F62.f-9 Step 3 with `PFX`. Scratch dirs stay until the Test Report of
   record; removing them is team-lead's call (disk-deletes rule).

## Pass

Steps 1–7 all hold, with no step between a launch and its evidence and no ship-gap fix anywhere in the run (A3).

**Deterministic oracles:** exit codes (`install_rc`, `ssh rc`), the broker audit, `sha256sum`, `wire list` JSON,
`plugins list`, `git rev-parse`/`find`.

**ENV:** a broker or lane error (timeout, image pull, prefix refused, GitHub fetch unreachable) is FAIL(env) and
stops.

## Evidence

`artifacts/scenario-AC-UPG-F69.s.a-18/`, holding:

- `evidence.txt` (`## <step> at=<UTC>`, command, `rc=`, key lines, tokens and secrets redacted);
- `broker-audit.log` for both install windows;
- `install-1.log` and `install-2.log`.
