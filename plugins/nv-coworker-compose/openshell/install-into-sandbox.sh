#!/usr/bin/env bash
# OSH-F64 two-phase installer entrypoint — a THIN bootstrap+delegate operator script run
# inside an EXISTING NemoClaw Hermes sandbox (staged in via `openshell sandbox create
# --upload` / `openshell sandbox exec`). All install LOGIC lives in the co-located pure
# planner `installer.py` (Hermes-native, hermetically testable); this script only routes.
#
#   install-into-sandbox.sh <coworker-types.yaml> --ref <40-char-sha> [--gateway-url <ws-url>] [--dry-run]
#   (a real run requires --gateway-url; --dry-run does not)
#
# --dry-run prints the FULL ordered transcript (Phase A plugin bootstrap + Phase B compose
# / profile installs / backed-up in-place default edit / managed fragment / wires /
# restart) by invoking the STAGED planner directly, so it needs neither `hermes coworker`
# (which does not exist until Phase A installs the plugins) nor a running gateway.
#
# The real run bootstraps the port's plugins (Phase A) so `hermes coworker` exists, then
# delegates Phase B to the `install-openshell` subaction, which re-plans idempotently
# (Phase A already satisfied → skipped) and applies the rest.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The planner needs PyYAML (a Hermes runtime dependency). A real NemoClaw sandbox's
# `python3` has it; when this script is run from a dev checkout it may not, so prefer an
# operator override, then the repo venv, then whatever `python3`/`python` can import yaml.
pick_python() {
  local py
  for py in "${HERMES_PYTHON:-}" "$DIR/../../../.venv/bin/python" "$DIR/../../../.venv/bin/python3" python3 python; do
    [ -n "$py" ] || continue
    if { command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; } && "$py" -c 'import yaml' >/dev/null 2>&1; then
      printf '%s\n' "$py"
      return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || { echo "install-into-sandbox.sh: no python with PyYAML found (set HERMES_PYTHON)" >&2; exit 3; }

SPEC=""
REF=""
DRY_RUN=0
GATEWAY_URL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:-}"; shift 2 ;;
    --gateway-url) GATEWAY_URL="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) if [ -z "$SPEC" ]; then SPEC="$1"; shift; else echo "unexpected arg: $1" >&2; exit 2; fi ;;
  esac
done

if [ -z "$SPEC" ] || [ -z "$REF" ]; then
  echo "usage: install-into-sandbox.sh <coworker-types.yaml> --ref <40-char-sha> [--gateway-url <ws-url>] [--dry-run]" >&2
  exit 2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  exec "$PY" "$DIR/installer.py" plan --spec "$SPEC" --ref "$REF"
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

# Real run — Phase A: bootstrap the port's plugins so `hermes coworker` exists (idempotent).
"$PY" "$DIR/installer.py" phase-a --spec "$SPEC" --ref "$REF"
# Phase B: compose + per-profile installs + backed-up in-place default edit + managed
# fragment + wires + rooms + restart, planned + applied by the install-openshell subaction.
exec hermes coworker install-openshell "$SPEC" --ref "$REF" --gateway-url "$GATEWAY_URL"
