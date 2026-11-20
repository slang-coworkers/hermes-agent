"""Managed-scope policy for OBS-F46's active-.env capture writer.

The ADR scopes the managed-scope behavior of ``apply_capture_env`` as a
code-review checkpoint rather than part of the three hermetic acceptance
criteria, so these live beside — not inside — the AC test and are NOT
``test_ac_obs_f46_*`` (they mint no acceptance-criterion id). They prove the
write-time policy contract: enabling capture (the operator's explicit opt-in) in a
coworker's active ``.env`` must never silently override machine-global managed
policy; the default (capture OFF) writes nothing new and consults no policy.

- A managed scope that already provides a truthy ``HERMES_DUMP_REQUESTS`` is a
  no-op success (the machine already enabled it; do not fight it).
- A managed scope that pins the key falsy, or a package-managed install that owns
  the environment, is an explicit operator action — surfaced as an error, never a
  silent local override.

The plugin is loaded through the real discovery path (as the AC test does) so the
behavior under test is the one that actually registers.
"""

from pathlib import Path
import os
import shutil

import pytest
import hermes_yaml as yaml

from hermes_cli import managed_scope
from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY

CAPTURE_KEY = "HERMES_DUMP_REQUESTS"


def _load(tmp_path, monkeypatch):
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
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded.module


def _managed_dir_with(tmp_path, monkeypatch, env_body):
    """Point HERMES_MANAGED_DIR at a tmp managed scope carrying *env_body*.

    HERMES_MANAGED_DIR is honored even under pytest (managed_scope.get_managed_dir
    resolves the override before the pytest guard), so this exercises the real
    managed-scope reader.
    """
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / ".env").write_text(env_body, encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    return managed


@pytest.mark.parametrize("truthy", ["true", "yes"])
def test_managed_truthy_is_noop_success(tmp_path, monkeypatch, truthy):
    module = _load(tmp_path, monkeypatch)
    _managed_dir_with(tmp_path, monkeypatch, f"{CAPTURE_KEY}={truthy}\n")

    profile = tmp_path / "prof"
    profile.mkdir()
    module.apply_capture_env(str(profile), enable=True)

    # Managed scope already enabled it machine-wide: no profile write at all.
    assert not (profile / ".env").exists()


def test_managed_falsy_raises_and_leaves_target_untouched(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    _managed_dir_with(tmp_path, monkeypatch, f"{CAPTURE_KEY}=false\n")

    profile = tmp_path / "prof"
    profile.mkdir()
    with pytest.raises(module.CaptureEnvManagedError):
        module.apply_capture_env(str(profile), enable=True)

    # A managed falsy pin is an administrative decision: no silent local override.
    assert not (profile / ".env").exists()


def test_managed_policy_read_failure_raises_and_leaves_target_untouched(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    # Invalid UTF-8 in the managed .env: read_text(encoding="utf-8") raises
    # UnicodeDecodeError, which managed_scope.load_managed_env swallows to {}
    # (fail-open). apply_capture_env must fail CLOSED rather than write the profile.
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / ".env").write_bytes(b"\xff\xfe " + CAPTURE_KEY.encode() + b"=true\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()

    profile = tmp_path / "prof"
    profile.mkdir()
    with pytest.raises(module.CaptureEnvManagedError):
        module.apply_capture_env(str(profile), enable=True)
    assert not (profile / ".env").exists()


def test_package_managed_install_raises_and_leaves_target_untouched(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    # No managed .env carries the key, but the install is package-managed.
    managed_scope.invalidate_managed_cache()
    monkeypatch.setenv("HERMES_MANAGED", "nixos")

    profile = tmp_path / "prof"
    profile.mkdir()
    with pytest.raises(module.CaptureEnvManagedError):
        module.apply_capture_env(str(profile), enable=True)

    assert not (profile / ".env").exists()


def test_opt_in_refusal_never_echoes_the_managed_value(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    sentinel = "sk-SENTINEL-not-a-real-credential-0123456789"
    _managed_dir_with(tmp_path, monkeypatch, f"{CAPTURE_KEY}={sentinel}\n")
    profile = tmp_path / "prof"
    profile.mkdir()
    with pytest.raises(module.CaptureEnvManagedError) as err:
        module.apply_capture_env(str(profile), enable=True)
    assert sentinel not in str(err.value)
    assert not (profile / ".env").exists()


# ---- the default: capture OFF, nothing new written, no policy consulted ----------------------


@pytest.mark.parametrize("managed_body", [
    f"{CAPTURE_KEY}=false\n", f"{CAPTURE_KEY}=true\n", f"{CAPTURE_KEY}=${{CAPTURE_ADMIN:-true}}\n", None,
])
def test_default_writes_nothing_whatever_the_managed_scope(tmp_path, monkeypatch, managed_body):
    module = _load(tmp_path, monkeypatch)
    if managed_body is not None:
        _managed_dir_with(tmp_path, monkeypatch, managed_body)
    monkeypatch.setenv("HERMES_MANAGED", "nixos")  # not consulted: nothing is enabled
    profile = tmp_path / "prof"
    profile.mkdir()
    module.capture_preflight(str(profile))
    module.apply_capture_env(str(profile))
    assert not (profile / ".env").exists()
    (profile / ".env").write_text("KEEP=1\n", encoding="utf-8")
    module.apply_capture_env(str(profile))
    assert (profile / ".env").read_text(encoding="utf-8") == "KEEP=1\n"


def test_default_removes_the_line_earlier_installers_wrote(tmp_path, monkeypatch):
    """Earlier versions appended exactly `HERMES_DUMP_REQUESTS=true`: the default removes every
    such line, keeping all other lines byte for byte (CRLF too) and the file mode."""
    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    env = profile / ".env"
    env.write_bytes(f"A=1\r\n{CAPTURE_KEY}=true\r\nB=2\r\n{CAPTURE_KEY}=true".encode("utf-8"))
    env.chmod(0o640)
    module.capture_preflight(str(profile))
    assert env.read_bytes() == b"A=1\r\nB=2\r\n"
    assert (env.stat().st_mode & 0o777) == 0o640
    module.apply_capture_env(str(profile))  # nothing left to remove: no write
    assert env.read_bytes() == b"A=1\r\nB=2\r\n"


@pytest.mark.parametrize("line", [
    f"{CAPTURE_KEY}=1", f"export {CAPTURE_KEY}=true", f"{CAPTURE_KEY}=true # debugging",
    f"  {CAPTURE_KEY}=true", f"{CAPTURE_KEY}=false",
])
def test_default_leaves_a_deliberate_operator_value_untouched(tmp_path, monkeypatch, line):
    """Any other form is not what an earlier installer wrote: it is the operator's explicit choice."""
    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    body = f"{line}\nKEEP=1\n"
    (profile / ".env").write_text(body, encoding="utf-8")
    module.apply_capture_env(str(profile))
    assert (profile / ".env").read_text(encoding="utf-8") == body


def test_default_cleans_the_way_the_loader_decodes(tmp_path, monkeypatch):
    """UTF-16 (BOM) and latin-1 files are decoded as the loader decodes them: the legacy line
    goes, every other byte (encoding, BOM) stays."""
    import codecs

    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    env = profile / ".env"
    env.write_bytes(codecs.BOM_UTF16_LE + f"A=1\n{CAPTURE_KEY}=true\n".encode("utf-16-le"))
    module.apply_capture_env(str(profile))
    assert env.read_bytes() == codecs.BOM_UTF16_LE + "A=1\n".encode("utf-16-le")

    env.write_bytes(b"# caf\xe9 notes\n" + f"{CAPTURE_KEY}=true\n".encode() + b"B=\xe9\n")
    module.apply_capture_env(str(profile))
    assert env.read_bytes() == b"# caf\xe9 notes\nB=\xe9\n"


def test_default_removes_only_top_level_legacy_bindings(tmp_path, monkeypatch):
    """A lookalike line inside a quoted multi-line value is data and stays; the BOM and CRLF stay."""
    import codecs

    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    env = profile / ".env"
    body = f'Q="x\n{CAPTURE_KEY}=true\ny"\r\n{CAPTURE_KEY}=true\r\nB=2\r\n'
    env.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
    module.apply_capture_env(str(profile))
    assert env.read_bytes() == codecs.BOM_UTF8 + f'Q="x\n{CAPTURE_KEY}=true\ny"\r\nB=2\r\n'.encode("utf-8")


def test_default_leaves_linked_env_files_alone(tmp_path, monkeypatch):
    """Earlier installers replaced <profile>/.env with a regular file of their own, so a symlink
    (e.g. an alias of the managed or launch .env) or a hard link cannot hold their line: such a
    file belongs to another scope and is never rewritten."""
    module = _load(tmp_path, monkeypatch)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    target = secrets / "shared.env"
    body = f"TOKEN_REF=x\n{CAPTURE_KEY}=true\n"
    target.write_text(body, encoding="utf-8")
    profile = tmp_path / "prof"
    profile.mkdir()
    (profile / ".env").symlink_to(target)
    module.apply_capture_env(str(profile))
    assert (profile / ".env").is_symlink()
    assert target.read_text(encoding="utf-8") == body

    (profile / ".env").unlink()
    os.link(target, profile / ".env")
    module.apply_capture_env(str(profile))
    assert target.read_text(encoding="utf-8") == body and os.stat(profile / ".env").st_nlink == 2


@pytest.mark.parametrize("nl", ["\n", "\r\n"])
def test_default_keeps_blank_lines_before_the_legacy_line(tmp_path, monkeypatch, nl):
    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    env = profile / ".env"
    env.write_bytes(f"A=1{nl}{nl}{CAPTURE_KEY}=true{nl}{nl}B=2{nl}".encode())
    module.apply_capture_env(str(profile))
    assert env.read_bytes() == f"A=1{nl}{nl}{nl}B=2{nl}".encode()


def test_default_clears_an_inherited_default_acl(tmp_path, monkeypatch):
    """A directory default ACL must not reach the rewritten .env when the original had none."""
    import shutil as _shutil
    import subprocess

    if not hasattr(os, "getxattr") or not _shutil.which("setfacl"):
        pytest.skip("no POSIX ACL tooling here")
    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    env = profile / ".env"
    env.write_text(f"{CAPTURE_KEY}=true\n", encoding="utf-8")
    env.chmod(0o600)
    if subprocess.run(["setfacl", "-d", "-m", "u:nobody:rw", str(profile)], capture_output=True).returncode != 0:
        pytest.skip("this filesystem does not take default ACLs")
    try:
        os.getxattr(env, "system.posix_acl_access")
        pytest.skip("the original already has an ACL")
    except OSError:
        pass
    module.apply_capture_env(str(profile))
    with pytest.raises(OSError):
        os.getxattr(env, "system.posix_acl_access")
    assert (env.stat().st_mode & 0o777) == 0o600 and env.read_text(encoding="utf-8") == ""


def test_default_rewrite_carries_the_posix_acl(tmp_path, monkeypatch):
    """The replacement file gets the original's POSIX ACL (and mode, owner) before it replaces it."""
    if not hasattr(os, "getxattr"):
        pytest.skip("no xattr API on this platform")
    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    env = profile / ".env"
    env.write_text(f"{CAPTURE_KEY}=true\n", encoding="utf-8")
    copied = []
    monkeypatch.setattr(os, "getxattr", lambda path, name: b"ACL-BYTES" if name == "system.posix_acl_access" else b"")
    monkeypatch.setattr(os, "setxattr", lambda path, name, value: copied.append((str(path), name, value)))
    module.apply_capture_env(str(profile))
    assert [(n, v) for _p, n, v in copied] == [("system.posix_acl_access", b"ACL-BYTES")]
    assert copied[0][0] != str(env)  # set on the staged file, before the replace
    assert env.read_text(encoding="utf-8") == ""


def test_default_refuses_when_the_legacy_line_cannot_be_removed(tmp_path, monkeypatch):
    """Capture would stay ON: an explicit operator action, raised before anything is installed."""
    module = _load(tmp_path, monkeypatch)
    profile = tmp_path / "prof"
    profile.mkdir()
    body = f"{CAPTURE_KEY}=true\n"
    (profile / ".env").write_text(body, encoding="utf-8")
    profile.chmod(0o555)  # the directory cannot take the atomic replace
    try:
        with pytest.raises(module.CaptureEnvManagedError):
            module.capture_preflight(str(profile))
    finally:
        profile.chmod(0o755)
    assert (profile / ".env").read_text(encoding="utf-8") == body

