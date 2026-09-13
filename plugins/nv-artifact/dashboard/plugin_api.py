"""nv-artifact dashboard backend — mounted at /api/plugins/nv-artifact/.

Reads the SAME single fleet ledger the CLI verbs read (pinned to
``ledger_profile``) and returns the outcome funnel, win-rate and cost-per-merge
for the operator. Reads are orchestrator-only: the same gate the CLI verbs
enforce, so a non-orchestrator profile gets ``authorized: False`` and no
aggregates.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - lets local unit tests import without core
    import os as _os

    def get_hermes_home() -> Path:  # type: ignore[misc]
        val = (_os.environ.get("HERMES_HOME") or "").strip()
        return Path(val) if val else Path.home() / ".hermes"

try:
    from fastapi import APIRouter
except Exception:  # pragma: no cover - allows import without dashboard deps
    class APIRouter:  # type: ignore
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def post(self, *_args, **_kwargs):
            return lambda fn: fn

router = APIRouter()


def _settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    entries = ((cfg.get("plugins") or {}).get("entries") or {}).get("nv-artifact") or {}
    settings = entries.get("settings") or {}
    return settings if isinstance(settings, dict) else {}


def _active_profile() -> Optional[str]:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return None


def _authorized() -> bool:
    orch = _settings().get("orchestrator_profile", "orchestrator")
    return _active_profile() == orch


def _ledger_path() -> Path:
    ledger_profile = _settings().get("ledger_profile", "default")
    try:
        from hermes_cli.profiles import get_profile_dir

        home = Path(get_profile_dir(ledger_profile))
    except Exception:
        home = Path(get_hermes_home())
    return home / "plugin-data" / "nv-artifact" / "data.db"


def _rows() -> List[Tuple[Optional[str], float]]:
    db = _ledger_path()
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return [
            (r[0], float(r[1] or 0.0))
            for r in conn.execute("SELECT terminal_outcome, cost_usd FROM outcomes")
        ]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _aggregates() -> Dict[str, Any]:
    rows = _rows()
    total = len(rows)
    merged = [r for r in rows if r[0] == "merged"]
    funnel: Dict[str, int] = {}
    for outcome, _cost in rows:
        key = outcome or "unknown"
        funnel[key] = funnel.get(key, 0) + 1
    winrate = (len(merged) / total) if total else 0.0
    merged_cost = sum(cost for _o, cost in merged)
    cost_per_merge = (merged_cost / len(merged)) if merged else 0.0
    return {
        "funnel": funnel,
        "winrate": winrate,
        "cost_per_merge": cost_per_merge,
        "merged": len(merged),
        "total": total,
    }


@router.get("/outcomes")
def outcomes() -> Dict[str, Any]:
    """Return the outcome funnel / win-rate / cost-per-merge for the operator."""
    if not _authorized():
        return {"authorized": False, "status": "refused",
                "reason": "outcomes analytics are orchestrator-only"}
    payload = _aggregates()
    payload["authorized"] = True
    return payload
