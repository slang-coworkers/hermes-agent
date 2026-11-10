#!/usr/bin/env bash
# OSH-F64 two-phase installer entrypoint — a THIN bootstrap+delegate operator script run
# inside an EXISTING NemoClaw Hermes sandbox (staged in via `openshell sandbox create
# --upload` / `openshell sandbox exec`). All install LOGIC lives in the co-located pure
# planner `installer.py` (Hermes-native, hermetically testable); this script only routes.
#
#   install-into-sandbox.sh <coworker-types.yaml> --ref <40-char-sha> [--gateway-url <ws-url>] [--policy-root <host dir>]
#                           [--fork-source <git url|path>] [--dry-run]
#                           [--openshell-mode local|remote] [--broker-addr tcp://<svc>.<ns>.svc:<port>]
#                           [--broker-token-file <path>] [--allow-plaintext-broker]
#   (a real run requires --gateway-url; --dry-run does not)
#
# --openshell-mode remote (or OPENSHELL_MODE=remote, or the spec's openshell.mode) targets
# an in-cluster OpenShell 0.1 gateway through the broker only; the default (local) path is
# unchanged. The mode flags are forwarded verbatim to the planner and the subaction, which resolve
# the mode (CLI > env > spec > local) — this script does not interpret them.
#
# --dry-run prints the FULL ordered transcript (Phase A plugin bootstrap + Phase B compose
# / profile installs / backed-up in-place default edit / managed fragment / wires /
# restart) by invoking the STAGED planner directly, so it needs neither `hermes coworker`
# (which does not exist until Phase A installs the plugins) nor a running gateway.
#
# The real run bootstraps the port's plugins (Phase A) so `hermes coworker` exists, then
# delegates Phase B to the `install-openshell` subaction, which re-plans idempotently
# (Phase A already satisfied → skipped) and applies the rest.
#
# OpenShell 0.1 (spec egress.openshell_api: "0.1"): the planner switches to the 0.1 CLI
# (create --detach -o json -- sleep infinity, explicit --provider, delete+wait for replace,
# `sandbox list -o json` with page tokens, the openshell-<name>.<workspace> ssh alias). Staging
# changes as well:
#   * `sandbox create --upload` cannot be combined with a trailing command in 0.1
#     (crates/openshell-cli/src/main.rs:1446-1453). Upload, then exec:
#       openshell sandbox create --name N --from <pullable ref> --detach -- sleep infinity
#       openshell sandbox upload N <this dir> /sandbox/osh-install
#       openshell sandbox exec --name N -- /sandbox/osh-install/install-into-sandbox.sh ...
#     Or bake the files into the image.
#   * Phase B restarts the Hermes gateway, which is a TCP server. The legacy seccomp mode
#     breaks TCP listeners inside 0.1 workloads, so a 0.1 fleet installs into the
#     hermes-gateway-agent pod (a plain Deployment), not into an OpenShell workload. The real run
#     refuses when it detects it is inside an OpenShell 0.1 workload (OPENSHELL_SANDBOX is set).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The planner needs Hermes' own `hermes_yaml` (Hermes 0.22 ships no PyYAML). A real NemoClaw
# sandbox's Hermes interpreter has it; a bare system `python3` does not, so prefer an operator
# override, then the repo venv, then whatever `python3`/`python` can import hermes_yaml.
pick_python() {
  local py
  for py in "${HERMES_PYTHON:-}" "$DIR/../../../.venv/bin/python" "$DIR/../../../.venv/bin/python3" python3 python; do
    [ -n "$py" ] || continue
    if { command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; } && "$py" -c 'import hermes_yaml' >/dev/null 2>&1; then
      printf '%s\n' "$py"
      return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || { echo "install-into-sandbox.sh: no python that can import hermes_yaml found (set HERMES_PYTHON)" >&2; exit 3; }

SPEC=""
REF=""
DRY_RUN=0
GATEWAY_URL=""
POLICY_ROOT=""
FORK_SOURCE=""
MODE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --policy-root) POLICY_ROOT="${2:-}"; shift 2 ;;
    --fork-source) FORK_SOURCE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --openshell-mode|--broker-addr|--broker-token-file)
      [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
      MODE_ARGS+=("$1" "$2"); shift 2 ;;
    --allow-plaintext-broker) MODE_ARGS+=("$1"); shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) if [ -z "$SPEC" ]; then SPEC="$1"; shift; else echo "unexpected arg: $1" >&2; exit 2; fi ;;
  esac
done

if [ -z "$SPEC" ] || [ -z "$REF" ]; then
  echo "usage: install-into-sandbox.sh <coworker-types.yaml> --ref <40-char-sha> [--gateway-url <ws-url>] [--policy-root <host dir>] [--fork-source <git url|path>] [--dry-run] [--openshell-mode local|remote] [--broker-addr <tcp://...>] [--broker-token-file <path>] [--allow-plaintext-broker]" >&2
  exit 2
fi

FORK_ARGS=()
[ -z "$FORK_SOURCE" ] || FORK_ARGS=(--fork-source "$FORK_SOURCE")
ROOT_ARGS=()
[ -z "$POLICY_ROOT" ] || ROOT_ARGS=(--policy-root "$POLICY_ROOT")

if [ "$DRY_RUN" -eq 1 ]; then
  exec "$PY" "$DIR/installer.py" plan --spec "$SPEC" --ref "$REF" ${ROOT_ARGS[@]+"${ROOT_ARGS[@]}"} ${FORK_ARGS[@]+"${FORK_ARGS[@]}"} \
    ${MODE_ARGS[@]+"${MODE_ARGS[@]}"}
fi

# A 0.1 fleet's gateway must not run inside an OpenShell workload (see header). The spec is read
# with hermes_yaml (PyYAML is not a core dependency); a spec that cannot be read refuses the run
# rather than reading as "not a 0.1 fleet".
if [ -n "${OPENSHELL_SANDBOX:-}" ]; then
  API_RC=0
  "$PY" - "$SPEC" <<'PYSPEC' || API_RC=$?
import sys

import hermes_yaml

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = hermes_yaml.safe_load(fh) or {}
    egress = spec.get("egress") or {}
    api = str(egress.get("openshell_api", "0.0"))
except Exception as exc:
    print(f"install-into-sandbox.sh: cannot read egress.openshell_api from the spec: {exc}", file=sys.stderr)
    sys.exit(3)
sys.exit(0 if api == "0.1" else 1)
PYSPEC
  case "$API_RC" in
    0) echo "install-into-sandbox.sh: openshell_api 0.1 fleets install into the hermes-gateway-agent pod, not an OpenShell workload (legacy seccomp mode breaks the gateway's TCP listener)" >&2
       exit 2 ;;
    1) ;;
    *) exit 2 ;;
  esac
fi

# Both phases operate on the sandbox's OWN Hermes home (the installer's _require_home()
# refuses to fall back to the platform default). Surface a missing HERMES_HOME here with a
# clear message rather than a mid-Phase-A traceback.
if [ -z "${HERMES_HOME:-}" ]; then
  echo "install-into-sandbox.sh: HERMES_HOME must be set to the target sandbox's Hermes home" >&2
  exit 2
fi
# Phase B probes and creates the fleet rooms against the LIVE gateway, so a real run needs a
# loopback gateway credential URL (unused by --dry-run above).
if [ -z "$GATEWAY_URL" ]; then
  echo "install-into-sandbox.sh: --gateway-url <loopback ?token/?ticket ws URL> is required for a real run" >&2
  exit 2
fi

# Real run — Phase A: write the §D6 scan guard (plugins.scan_on_install: false into the managed
# config) BEFORE the first `hermes plugins install`, then bootstrap the port's plugins so `hermes
# coworker` exists. phase-a runs the build_plan steps in order (backup -> scan_guard_write ->
# plugin installs), so the guard is in place before any scanned install — the install-time scanner
# returns a DANGEROUS verdict on the first-party fleet plugins that --force cannot override, so an
# install attempted before the guard would exit 1. Idempotent — a re-run skips all three.
"$PY" "$DIR/installer.py" phase-a --spec "$SPEC" --ref "$REF" ${FORK_ARGS[@]+"${FORK_ARGS[@]}"} \
  ${MODE_ARGS[@]+"${MODE_ARGS[@]}"}
# Phase B: compose + per-profile installs + backed-up in-place default edit + managed
# fragment + wires + rooms + restart, planned + applied by the install-openshell subaction.
exec hermes coworker install-openshell "$SPEC" --ref "$REF" --gateway-url "$GATEWAY_URL" \
  ${ROOT_ARGS[@]+"${ROOT_ARGS[@]}"} ${FORK_ARGS[@]+"${FORK_ARGS[@]}"} ${MODE_ARGS[@]+"${MODE_ARGS[@]}"}
