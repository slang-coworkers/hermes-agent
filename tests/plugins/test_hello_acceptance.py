"""Acceptance test for the ``hello`` plugin (requirement P0-LOOP).

Extends the P1-HELLO hook-only scaffold's contract: the plugin still registers
exactly one ``post_tool_call`` observer hook whose firing appends one line to
the plugin's own durable storage under ``<HERMES_HOME>/plugin-data/hello/``,
and it now ALSO registers a ``/hello`` slash command whose handler returns a
greeting naming the active profile.

``test_ac_p0_loop_1`` is the single ``pytest:`` acceptance criterion
(AC-P0-LOOP-1); the remaining tests are the retained P1 behavioural contract,
with the "no command" ownership assertion updated for the added command.
"""

import hashlib
import json
import logging
import os
import shutil
import stat
import threading
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / "hello"

PLUGIN_KEY = "hello"
HOOK_NAME = "post_tool_call"
COMMAND_NAME = "hello"
LOG_FILENAME = "hello.log"

# Matches _PROFILE_ID_RE = ^[a-z0-9][a-z0-9_-]{0,63}$ (hermes_cli/profiles.py:51).
PROFILE_NAME = "p0-loop-bot"
EXPECTED_GREETING = f"Hello from {PROFILE_NAME}!"

# Registration kinds the LOADER attributes to a plugin key without the plugin
# asking: tracked before register() is ever called, so they are host bookkeeping
# rather than part of the plugin's declared surface.
HOST_INJECTED_KINDS = {"tool_override_policy"}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with ``hello`` installed as a USER plugin.

    Installing it outside the bundled dir — and pointing HERMES_BUNDLED_PLUGINS
    at an empty dir — is what makes this exercise discovery on a stock tree
    rather than passing because the plugin happens to sit in the repo.
    """
    assert PLUGIN_SRC.is_dir(), f"hello plugin not found at {PLUGIN_SRC}"

    # tests/conftest.py already points HERMES_HOME at tmp_path/"hermes_test";
    # a different subdir keeps the two from aliasing.
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)

    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}),
        encoding="utf-8",
    )

    # Must be a real path: an empty string is falsy where Hermes reads this var,
    # which would silently fall back to the in-repo plugins/ dir and load the
    # entire bundled set.
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    return home


@pytest.fixture
def manager(hermes_home):
    """A loaded manager.

    Constructed only after HERMES_HOME is set, because PluginManager captures
    its home immutably at construction time.
    """
    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()
    yield mgr
    mgr.unload()


@pytest.fixture
def profile_manager(tmp_path, monkeypatch):
    """A loaded manager whose HERMES_HOME resolves to profile ``PROFILE_NAME``.

    ``get_active_profile_name()`` derives the name from HERMES_HOME via the
    *profiles root*, which is anchored to the DEFAULT hermes root, not to
    HERMES_HOME itself:

    * ``get_default_hermes_root()`` returns ``~/.hermes`` when HERMES_HOME is
      *under* ``~/.hermes`` (hermes_constants.py:183-217), so
    * ``_get_profiles_root()`` == ``~/.hermes/profiles`` (profiles.py:282-293),
      and ``get_active_profile_name()`` returns the trailing path part when
      HERMES_HOME == ``<root>/profiles/<name>`` (profiles.py:2087-2111).

    Hence HOME is rooted in tmp_path and HERMES_HOME is
    ``<HOME>/.hermes/profiles/<PROFILE_NAME>``. The plugin is installed as a
    USER plugin with an EMPTY bundled dir, so discovery is exercised on a stock
    tree rather than passing because the plugin sits in the repo.
    """
    assert PLUGIN_SRC.is_dir(), f"hello plugin not found at {PLUGIN_SRC}"

    home_root = tmp_path / "os-home"
    hermes_home = home_root / ".hermes" / "profiles" / PROFILE_NAME
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}),
        encoding="utf-8",
    )

    # Must be a real path: an empty string is falsy where Hermes reads this var
    # and silently falls back to the in-repo plugins/ dir, loading the whole
    # bundled set and defeating the isolation.
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()

    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    # Construct AFTER HERMES_HOME is set — PluginManager captures its home
    # immutably at construction time.
    from hermes_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load()
    yield mgr
    mgr.unload()


def _log_path():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_KEY) / LOG_FILENAME


def _fire(mgr, tool_name):
    """Drive the hook with a production-shaped payload plus one additive field.

    ``future_additive_field`` is not part of the real payload. It exercises the
    additive-payload path, but note what that does and does not prove: Hermes
    filters unknown keys out for narrow callbacks, so this passes with or
    without ``**kwargs`` on the callback. The ``**kwargs`` requirement itself is
    enforced by ``plugins doctor``, not here.
    """
    return mgr.invoke_hook(
        HOOK_NAME,
        tool_name=tool_name,
        args={"command": "echo hi"},
        result=json.dumps({"ok": True}),
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        api_request_id="req-1",
        duration_ms=12,
        status="ok",
        error_type=None,
        error_message=None,
        middleware_trace=[],
        future_additive_field={"nested": "value"},
    )


def _lines():
    return [ln for ln in _log_path().read_text(encoding="utf-8").splitlines() if ln.strip()]


def _install_tree_entries(root):
    """Yield ``(relative path, lstat result)`` for every entry under *root*.

    An explicit scandir walk rather than ``rglob``: this must not follow
    directory symlinks, and pathlib's behaviour there varies across the
    supported Python versions.
    """
    stack = [root]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                yield str(path.relative_to(root)), entry.stat(follow_symlinks=False)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)


def _node_fingerprint(path, mode):
    """Node type plus content for one entry, given its ``lstat`` mode."""
    if stat.S_ISLNK(mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISREG(mode):
        return ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    if stat.S_ISDIR(mode):
        return ("dir", None)
    return (f"mode:{stat.S_IFMT(mode)}", None)


def _install_tree_fingerprint(plugin_dir):
    """Snapshot the install root and every entry under it.

    Records, per entry: relative path, node type, regular-file content hash, and
    symlink target. Not metadata, not timestamps.

    Keyed on ``lstat`` rather than ``is_file`` because a plugin can mutate its
    install tree without changing any file's bytes: creating an empty directory,
    swapping a regular file for a symlink to identical content, or changing a
    node's type. The root is recorded under "." and descended into only when it
    is genuinely a directory — otherwise replacing the plugin dir itself with a
    symlink to a copy would reproduce the same child fingerprint.
    """
    root_mode = plugin_dir.lstat().st_mode
    fingerprint = {".": _node_fingerprint(plugin_dir, root_mode)}
    if stat.S_ISDIR(root_mode):
        for rel, st in _install_tree_entries(plugin_dir):
            fingerprint[rel] = _node_fingerprint(plugin_dir / rel, st.st_mode)
    return fingerprint


def test_hello_plugin_is_discovered_and_loaded(manager):
    loaded = manager._plugins[PLUGIN_KEY]

    assert loaded.error is None, f"plugin failed to load: {loaded.error}"
    assert loaded.enabled is True
    assert loaded.module is not None


def test_manifest_declares_and_is_loaded_as_version_1(manager, hermes_home):
    """The requirement asks for an explicit ``manifest_version: 1``.

    The loader defaults a missing key to 1, so asserting the parsed value alone
    would also pass with no key at all — hence both halves.
    """
    declared = yaml.safe_load(
        (hermes_home / "plugins" / PLUGIN_KEY / "plugin.yaml").read_text(encoding="utf-8")
    )
    assert declared["manifest_version"] == 1
    assert manager._plugins[PLUGIN_KEY].manifest.manifest_version == 1


def test_registers_one_hook_and_one_command_and_nothing_else(manager):
    """One hook AND one slash command, and no other registration.

    P0-LOOP supersedes the P1-HELLO "no command" assertion: the plugin now owns
    exactly the ``post_tool_call`` observer hook and the ``/hello`` slash
    command. Asserted against the per-plugin ownership ledger, which is broader
    than any single registry and — unlike ``_registration_order`` — also carries
    ``persistent`` registrations. Excluding HOST_INJECTED_KINDS keeps it strict
    rather than weakening it: a tool, middleware, second hook, or second command
    still fails this. Ledger order follows registration order, and register()
    registers the hook before the command.
    """
    owned = [
        registration
        for registration in manager._ownership_ledger.get(PLUGIN_KEY, [])
        if registration.active and registration.kind not in HOST_INJECTED_KINDS
    ]
    assert [(r.kind, r.key) for r in owned] == [
        ("hook", HOOK_NAME),
        ("command", COMMAND_NAME),
    ]

    loaded = manager._plugins[PLUGIN_KEY]
    # List equality, not set: a duplicate registration of the same hook/command
    # would survive a set comparison and still violate "exactly one".
    assert loaded.hooks_registered == [HOOK_NAME]
    assert loaded.commands_registered == [COMMAND_NAME]
    assert not loaded.tools_registered
    assert not loaded.middleware_registered

    # CLI subcommands live in a separate registry, so commands_registered alone
    # does not cover "no CLI command".
    plugin_cli = [
        entry
        for entry in manager._cli_commands.values()
        if entry.get("plugin_key") == PLUGIN_KEY
    ]
    assert not plugin_cli


def test_manifest_declares_the_hook_under_provides_hooks(manager):
    """`provides_hooks:` is the only key the parser reads — `hooks:` is ignored.

    Load-bearing: every bundled manifest that declares hooks at all uses
    `hooks:`, and none uses `provides_hooks:`, so "correcting" this plugin to
    match them would leave provides_hooks empty. Doctor downgrades that drift to
    a warning and `--ci` ignores warnings, so this is the only enforcing gate.
    """
    assert manager._plugins[PLUGIN_KEY].manifest.provides_hooks == [HOOK_NAME]


def test_hook_appends_line_to_plugin_storage(manager):
    # Only the FILE is absent: _log_path() resolves plugin_data_dir(), which
    # mkdirs the data root, so the directory already exists by this line.
    log = _log_path()
    assert not log.exists(), "log file should not exist yet"

    _fire(manager, "shell")

    assert log.is_file(), f"observer hook wrote nothing to {log}"
    lines = _lines()
    assert len(lines) == 1
    assert json.loads(lines[0])["tool_name"] == "shell"


def test_hook_appends_rather_than_overwrites(manager):
    _fire(manager, "shell")
    _fire(manager, "read_file")

    lines = _lines()
    assert len(lines) == 2
    assert [json.loads(ln)["tool_name"] for ln in lines] == ["shell", "read_file"]


def test_durable_state_lives_outside_the_plugin_install_tree(manager, hermes_home):
    """Durable state belongs in plugin-data, not the install tree that
    ``plugins remove`` deletes.

    Load-bearing: drop the is_file() assertion and the location checks decay
    into restatements of what plugin_data_dir() returns, true even for a plugin
    writing into its own install tree.
    """
    plugin_dir = hermes_home / "plugins" / PLUGIN_KEY
    before = _install_tree_fingerprint(plugin_dir)

    _fire(manager, "shell")

    log = _log_path()
    assert log.is_file(), f"plugin did not write to {log}"
    assert (hermes_home / "plugins") not in log.parents
    assert log.parent == hermes_home / "plugin-data" / PLUGIN_KEY
    # Writing the right file does not excuse also writing into the install tree.
    # Bounded claim: this compares persistent path, node type, regular-file
    # content and symlink target — not metadata, and not a write-then-restore
    # inside the window.
    assert _install_tree_fingerprint(plugin_dir) == before


def test_observer_contributes_no_decision_value(manager, caplog, monkeypatch):
    """An observer must not fail and must return nothing a caller could act on.

    invoke_hook collects only non-None returns AND suppresses callback
    exceptions, so an empty result is equally consistent with a crashed
    callback. Two invocations are needed to close that gap, because the two
    failure classes surface on different paths:

    * ``Exception`` — swallowed and logged, so the default (threaded) path is
      checked against the suppression warnings.
    * ``BaseException`` — the worker catches only ``Exception``
      (plugins.py:5655) yet still sets the done event (:5661), so SystemExit
      leaves no warning and no return value. Disabling the hook timeout routes
      the call inline (:5689), where the outer ``except Exception`` (:5692)
      cannot swallow it and it propagates into this test.
    """
    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        assert _fire(manager, "shell") == []

        # Second invocation, synchronous: <= 0 disables the threaded timeout, so
        # a BaseException from the callback reaches this frame instead of dying
        # in the worker thread.
        monkeypatch.setattr(
            "hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.0
        )

        # Prove the synchronous leg actually ran, rather than trusting that the
        # patch took effect. setattr gives only partial protection: a RENAMED
        # resolver fails loudly here (raising=True cannot find the attribute), but
        # a call site that no longer consults it leaves the symbol in place, so the
        # patch succeeds while changing nothing. The positive default timeout then
        # stays in force and the callback runs on a worker thread, which is what
        # the thread-id assertion below detects. It covers that case, not every
        # possible refactor of the call site. _invoke_hook_callback is a
        # @staticmethod, so the replacement takes (callback, payload) and must be
        # re-wrapped.
        original = type(manager)._invoke_hook_callback
        invoked_on = []

        def _record(callback, payload):
            invoked_on.append(threading.get_ident())
            return original(callback, payload)

        monkeypatch.setattr(
            type(manager), "_invoke_hook_callback", staticmethod(_record)
        )
        assert _fire(manager, "shell") == []
        assert invoked_on == [threading.get_ident()]

    assert _log_path().is_file()

    # Checked after BOTH invocations: an Exception raised only on the second one
    # is swallowed at plugins.py:5692 and would otherwise go unexamined.
    # at_level() enables the level but does not filter caplog.records, so the
    # predicate must pin the logger itself. Every warning this logger emits with
    # this prefix signals callback failure or suppression, so match the prefix
    # rather than individual message forms.
    suppressed = [
        record.getMessage()
        for record in caplog.records
        if record.name == "hermes_cli.plugins"
        and record.getMessage().startswith(f"Hook '{HOOK_NAME}' callback ")
    ]
    assert not suppressed, f"observer failed and Hermes suppressed it: {suppressed}"


def test_ac_p0_loop_1(profile_manager):
    """AC-P0-LOOP-1: The `hello` plugin, loaded through the real discovery path, registers a `/hello` slash command whose handler returns exactly `Hello from <profile>!` naming the active profile."""
    loaded = profile_manager._plugins[PLUGIN_KEY]
    assert loaded.error is None, f"plugin failed to load: {loaded.error}"
    assert loaded.enabled is True

    # The command must be registered on THIS manager instance. Read the entry
    # directly (there is no manager.get_plugin_commands() instance method, and
    # the module-level get_plugin_command_handler uses a separate cached global
    # manager). Entry shape: hermes_cli/plugins.py:2235-2243.
    assert PLUGIN_KEY in profile_manager._plugin_commands, "/hello not registered"
    entry = profile_manager._plugin_commands[PLUGIN_KEY]
    assert entry["plugin_key"] == PLUGIN_KEY

    # Sanity-check the profile the fixture set up is the one the accessor sees,
    # so the greeting assertion below is meaningful rather than accidental.
    from hermes_cli.profiles import get_active_profile_name

    assert get_active_profile_name() == PROFILE_NAME

    # The command handler is called with a single positional (args after the
    # command name); it returns the reply string. Exact equality — the greeting
    # is a defined string, not a fuzzy match.
    reply = entry["handler"]("")
    assert reply == EXPECTED_GREETING, f"unexpected greeting: {reply!r}"
