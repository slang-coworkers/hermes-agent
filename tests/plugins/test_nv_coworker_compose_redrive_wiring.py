"""UPG-F68 erratum E11: the composed fleet enables nv-delivery-redrive (FLEET-F62.h) on every profile.

``compose()`` is loaded through the real plugin discovery path and driven on a two-type spec; the
render is read back from disk. The spec helpers and the ``loaded`` fixture are the session-modes
acceptance test's own.
"""

from __future__ import annotations

from tests.plugins import test_nv_coworker_compose_session_modes_acceptance as _modes

loaded = _modes.loaded  # registers the session-modes test's fixture in this module

REDRIVE = "nv-delivery-redrive"


def test_every_rendered_profile_enables_redrive_with_the_orchestrator(loaded, tmp_path):
    module, _entry = loaded
    out = _modes._render(module, tmp_path, _modes._base_spec(), "redrive")
    for profile in ("default", "orchestrator", "worker"):
        plugins = _modes._profile_config(out, profile).get("plugins") or {}
        assert REDRIVE in (plugins.get("enabled") or []), profile
        assert plugins["entries"][REDRIVE]["settings"]["orchestrator_profile"] == "orchestrator", profile


def test_a_spec_that_already_enables_redrive_gets_it_once(loaded, tmp_path):
    module, _entry = loaded
    spec = _modes._base_spec()
    spec["types"]["worker"]["config"] = {"plugins": {"enabled": [REDRIVE]}}
    out = _modes._render(module, tmp_path, spec, "once")
    assert (_modes._profile_config(out, "worker")["plugins"]["enabled"]).count(REDRIVE) == 1
