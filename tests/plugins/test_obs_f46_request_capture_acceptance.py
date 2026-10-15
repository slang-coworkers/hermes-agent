"""Behavior-contract acceptance tests for OBS-F46 — raw request/response capture.

OBS-F46 turns on Hermes's built-in raw request capture (``HERMES_DUMP_REQUESTS``,
read process-global via ``env_var_enabled`` -> ``os.getenv``) across the fleet by
extending the nv-coworker-compose plugin. Because the toggle is read from
``os.environ`` and a profile's ``.env`` reaches ``os.environ`` only for
separately-spawned ``hermes -p <bot> chat`` processes (kanban workers, cron
bot-chat) — the in-process multiplex path isolates a secondary profile's ``.env``
in a secret scope — activation has two surfaces:

  1. Each coworker profile's ACTIVE ``.env`` — the file loaded into ``os.environ``
     at ``hermes -p <bot>`` startup — which the plugin idempotently upserts at
     install.
  2. The gateway PROCESS env (launch/DEFAULT active ``.env``, service-unit env, or
     managed ``/etc/hermes/.env``) — an operator/runbook step for gateway-served
     turns.

The rendered ``.env.template`` is renamed to the inert ``.env.EXAMPLE`` on install
and is never loaded, so it is the documented default only; the active-``.env``
upsert is the activation. These tests load the plugin from an isolated
``HERMES_HOME`` through the real discovery path (``HERMES_BUNDLED_PLUGINS`` at an
empty dir) and drive the plugin's render/install seams plus the runbook page and
the active-``.env`` -> ``os.environ`` mechanism. Values (``HERMES_DUMP_REQUESTS``,
``true``) are the requirement's, not read from the plugin, keeping the test
non-circular.
"""

from pathlib import Path
import os
import re
import shutil
import stat

import yaml

from hermes_cli.plugins import PluginManager
from hermes_cli.env_loader import load_hermes_dotenv

PLUGIN_KEY = "nv-coworker-compose"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "obs-f46"
SPEC = FIXTURE_DIR / "coworker-types.yaml"

CAPTURE_KEY = "HERMES_DUMP_REQUESTS"
CAPTURE_VALUE = "true"

# The fleet raw-request-capture runbook (the gateway-process-env mechanism cannot
# be rendered into a profile, so it is documented here).
RUNBOOK_PATH = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-raw-request-capture.md"

# One assignment per line, honoring leading whitespace, an ``export`` prefix
# separated by any whitespace, and whitespace around ``=``. len() is the
# assignment COUNT, so a canonical writer must yield exactly one.
_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _dig(mapping, dotted):
    node = mapping
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return (False, None)
        node = node[key]
    return (True, node)


def _write_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}),
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


def _load(tmp_path, monkeypatch):
    _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return manager, loaded


def _render(loaded, spec, out_root):
    return loaded.module.compose(str(spec), str(out_root))


def _raw_config(profile_dir):
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _default_profile_name(rendered):
    for name, profile_dir in rendered.items():
        found, val = _dig(_raw_config(profile_dir), "gateway.multiplex_profiles")
        if found and val is True:
            return name
    raise AssertionError("no rendered profile carries gateway.multiplex_profiles: true")


def _env_assignments(path, key):
    values = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGN_RE.match(raw)
        if match and match.group(1) == key:
            values.append(match.group(2).strip().strip('"').strip("'"))
    return values


def test_ac_obs_f46_1(tmp_path, monkeypatch):
    """AC-OBS-F46-1: The compose render writes exactly one HERMES_DUMP_REQUESTS=true
    line into the .env.template of every rendered profile — the DEFAULT (multiplexer)
    profile and every coworker — so the distribution's documented env default carries
    the capture toggle. Asserted on the RAW rendered .env.template, parsed as dotenv."""
    _, loaded = _load(tmp_path, monkeypatch)
    rendered = _render(loaded, SPEC, tmp_path / "out")
    default_name = _default_profile_name(rendered)
    coworkers = [n for n in rendered if n != default_name]
    assert coworkers, "fixture must render at least one coworker profile"
    for name, profile_dir in rendered.items():
        template = Path(profile_dir) / ".env.template"
        assert template.is_file(), f"{name}: .env.template not rendered"
        values = _env_assignments(template, CAPTURE_KEY)
        assert values == [CAPTURE_VALUE], (
            f"{name}: .env.template must carry exactly one {CAPTURE_KEY}={CAPTURE_VALUE}, "
            f"got {values!r}"
        )


def test_ac_obs_f46_2(tmp_path, monkeypatch):
    """AC-OBS-F46-2: Installing a coworker profile idempotently sets exactly one
    HERMES_DUMP_REQUESTS=true in that profile's ACTIVE .env (the file a spawned
    `hermes -p <bot> chat` loads into os.environ) — inserting it when absent,
    collapsing/overriding any prior conflicting assignments, preserving unrelated
    entries and the file mode, stable across repeated application, and WITHOUT
    mutating the calling process's os.environ."""
    _, loaded = _load(tmp_path, monkeypatch)
    apply_capture_env = loaded.module.apply_capture_env

    # A caller-process sentinel captured BEFORE any apply: enabling a profile's .env
    # must never mutate the calling process's os.environ (save_env_value would; the
    # plugin-local writer must not). Capturing up front catches a first-call mutation.
    monkeypatch.setenv(CAPTURE_KEY, "caller-sentinel")
    caller_env_before = os.environ[CAPTURE_KEY]

    home_insert = tmp_path / "prof-insert"
    home_insert.mkdir()
    apply_capture_env(str(home_insert))
    assert _env_assignments(home_insert / ".env", CAPTURE_KEY) == [CAPTURE_VALUE]
    assert os.environ.get(CAPTURE_KEY) == caller_env_before

    # Hostile pre-seed spans the assignment forms the writer must collapse to one:
    # leading whitespace, a tab-separated `export`, and spaces around `=`.
    home_hostile = tmp_path / "prof-hostile"
    home_hostile.mkdir()
    hostile_env = home_hostile / ".env"
    hostile_env.write_text(
        "  HERMES_DUMP_REQUESTS=false\n"
        "\texport\tHERMES_DUMP_REQUESTS = 0\n"
        "KEEP_ME=sentinel\n",
        encoding="utf-8",
    )
    hostile_env.chmod(0o640)  # distinct from tempfile's 0o600 default on POSIX
    mode_before = stat.S_IMODE(hostile_env.stat().st_mode)  # platform-representable observed mode
    apply_capture_env(str(home_hostile))
    assert _env_assignments(hostile_env, CAPTURE_KEY) == [CAPTURE_VALUE], (
        "hostile .env: writer must collapse prior assignments to exactly one true"
    )
    assert _env_assignments(hostile_env, "KEEP_ME") == ["sentinel"], (
        "writer must preserve unrelated entries"
    )
    # Preserve the mode the platform actually stored (on POSIX an atomic tempfile
    # replace that drops the original mode surfaces here; portable to the Windows lane).
    assert stat.S_IMODE(hostile_env.stat().st_mode) == mode_before, "writer must preserve file mode"
    assert os.environ.get(CAPTURE_KEY) == caller_env_before

    # A second application must be byte-for-byte stable, not merely semantically equal.
    bytes_after_first = hostile_env.read_bytes()
    apply_capture_env(str(home_hostile))
    assert hostile_env.read_bytes() == bytes_after_first
    assert os.environ.get(CAPTURE_KEY) == caller_env_before

    # _install (install_distribution + the upsert) returns the installed home; this
    # proves install wires apply_capture_env without leaking to the caller's os.environ.
    rendered = _render(loaded, SPEC, tmp_path / "out")
    default_name = _default_profile_name(rendered)
    coworker = next(n for n in rendered if n != default_name)
    installed_home = loaded.module._install(str(rendered[coworker]), coworker)
    assert _env_assignments(Path(installed_home) / ".env", CAPTURE_KEY) == [CAPTURE_VALUE], (
        f"installed coworker {coworker}: active .env must carry {CAPTURE_KEY}={CAPTURE_VALUE}"
    )
    assert os.environ.get(CAPTURE_KEY) == caller_env_before


def test_ac_obs_f46_3(tmp_path, monkeypatch):
    """AC-OBS-F46-3: The fleet raw-request-capture runbook documents the gateway
    PROCESS-env activation (launch/DEFAULT active .env, service-unit Environment=,
    launchd EnvironmentVariables, or managed /etc/hermes/.env), the secret-scope
    reason a secondary profile's .env cannot enable gateway-served capture, the
    restart requirement, and the retention dependency (sessions.auto_prune: false,
    MEM-F44) — and that documented active-.env -> os.environ mechanism is proven
    behaviorally, so the criterion is not prose-only."""
    assert RUNBOOK_PATH.is_file(), f"runbook page absent: {RUNBOOK_PATH}"
    norm = RUNBOOK_PATH.read_text(encoding="utf-8").replace("`", "").lower()
    required = [
        "hermes_dump_requests=true",
        "launch/default",                # disambiguated from the "launchd" substring
        "active .env",
        "environment=",
        "environmentvariables",
        "/etc/hermes/.env",
        "secondary",
        "secret scope",                  # why a secondary profile's .env cannot enable it
        "restart",
        "sessions.auto_prune: false",    # the sessions key, not checkpoints.auto_prune
        "mem-f44",                       # ...which owns that retention key
    ]
    missing = [needle for needle in required if needle not in norm]
    assert not missing, f"runbook missing required operational facts: {missing}"

    # Proves the runbook's primary mechanism is real: an active .env carrying the
    # toggle reaches os.environ on load (the path the gateway launch and every
    # `hermes -p <bot>` process take).
    home = tmp_path / "mech-home"
    home.mkdir()
    (home / ".env").write_text(f"{CAPTURE_KEY}={CAPTURE_VALUE}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    prior = os.environ.get(CAPTURE_KEY)
    try:
        load_hermes_dotenv(hermes_home=str(home), load_external_secrets=False)
        assert os.environ.get(CAPTURE_KEY) == CAPTURE_VALUE, (
            "an active .env carrying the toggle must place it into os.environ"
        )
    finally:
        if prior is None:
            os.environ.pop(CAPTURE_KEY, None)
        else:
            os.environ[CAPTURE_KEY] = prior
