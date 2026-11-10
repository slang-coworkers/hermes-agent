---
ac: AC-UNIFY-F70-5
kind: live
model: live
base_url: recorded-at-run
api_mode: recorded-at-run
model_id: recorded-at-run
fixtures: []
timeout_s: 600
---

# AC-UNIFY-F70-5 — a candidate tag cut from the PR head installs pinned and serves one probe turn with no refusals

On the e2e box, a candidate tag cut from the PR head is installed over the shipped fleet. nv-fleet-gates,
nv-coworker-compose and nv-artifact then read back pinned to that head, and one triager probe turn hands off to
its worker with no nv-fleet-gates refusal in either profile. `model: live`: the run uses the shipped fleet's
active model route. The operator who runs the box records `base_url`, `api_mode`, `model_id` and `key_env` from
the box's own provider block in the private run evidence. This file keeps them as `recorded-at-run`, and no route
is assumed.

The operator runs it on the box in one window of at most 2 h, with one row on the box at a time. The tester audits
the evidence the operator returns and does not run on the box. There are no in-repo fixtures: the profiles are the
box's shipped triager and worker.

## Setup

1. Disk gate: `df -h` on the box shows at least 20 GB free and at most 92 % used. Otherwise stop. Any delete to
   free space is asked for first, never done.
2. Record the shipped tag (the rollback target) and `HEAD`, the PR head sha.
3. Cut the candidate tag from `HEAD` and install it with the box's install procedure, which uses the
   `hermes plugins install … --ref <HEAD>` form that writes `pinned: true, revision: <HEAD>` into each plugin's
   `.install-metadata.json`.
4. Note `T0`, the UTC time the probe starts.

## Steps

1. Pin read-back: run `hermes plugins list` in the installed home → expect: the Source column of nv-fleet-gates,
   nv-coworker-compose and nv-artifact reads `git pinned@<HEAD[:8]>`, and a `python3 -c` read of each plugin's
   `.install-metadata.json` gives `revision == HEAD` and `pinned is True`.
2. Probe: send the triager its probe message → expect: one triager turn whose hand-off reaches the worker, and the
   worker's `$HERMES_HOME/profiles/<worker>/state.db` holds the session that turn opened (a `python3 -c` sqlite3
   query; the image has no `sqlite3` CLI).
3. Refusal count → expect: 0 nv-fleet-gates refusals in the tool results of both profiles' `state.db` from `T0` to
   the end of the probe, counted by the refusal text the gate emits.
4. Rollback: reinstall the shipped tag → expect: `hermes plugins list` shows the shipped pins again.

## Pass

Steps 1-3 hold on the first probe. A miss on model wording, format or timing is a WATCH item, not a counted FAIL. A
miss on the pin read-back, or a refusal count above zero, is a FAIL.

## Evidence

- The `hermes plugins list` output and the `.install-metadata.json` read-backs (step 1).
- The `state.db` query outputs for steps 2 and 3, with the query text.
- The box's provider block as read on the box (`base_url`, `api_mode`, `model_id`, `key_env`), kept in the private
  run evidence only.
- The disk-gate output and the rollback read-back (step 4).
