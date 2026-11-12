"""Guardrails of the UPG-F69.s.a installer steps that the acceptance rows do not pin: route-script binding and
containment, the ``ssh_lane`` path alphabet, the strict wire-manifest parse and a ``--fork-source`` echoed without
its userinfo; the AC-20 live probe's session records landing in the store its pass's profile scope reads; and the
approve/merge detector reading line continuations and operators glued to a flag."""

import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "plugins" / "nv-coworker-compose" / "openshell" / "installer.py"
PROBE = REPO_ROOT / "tests" / "e2e-scenarios" / "UPG-F69.s.a" / "ac20_probe.py"
STORES = REPO_ROOT / "plugins" / "nv-fleet-gates" / "stores.py"
PREDICATES = REPO_ROOT / "plugins" / "nv-fleet-gates" / "predicates.py"
FORMS = json.loads((PROBE.parent / "ac20-forms.json").read_text(encoding="utf-8"))
SPEC = {"types": {"orchestrator": {}, "triager": {}}, "default_profile": "default"}
LANE = {"bundle": "/srv/osh-lane", "env_file": "lane.env", "shim": "bin/openshell",
        "interpreter": "/opt/hermes/.venv/bin/python3", "members": ["token"]}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def installer():
    return _load(INSTALLER, "upg_f69sa_guardrails_installer")


def _routes(**routes):
    return {"platforms": {"webhook": {"extra": {"routes": routes}}}}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "relay.py").write_text("print('relay')\n", encoding="utf-8")
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    return home


def test_route_scripts_non_roster(installer, tmp_path):
    """A route bound to an installed profile outside the roster gets its script; an unknown profile refuses."""
    home = _home(tmp_path)
    (home / "profiles" / "ops").mkdir(parents=True)
    copies = installer._route_script_copies(home, tmp_path / "render", SPEC,
                                            _routes(ops={"profile": "ops", "script": "relay.py"}))
    installer._place_route_scripts(copies)
    placed = home / "profiles" / "ops" / "scripts" / "relay.py"
    assert placed.read_bytes() == (home / "scripts" / "relay.py").read_bytes()
    with pytest.raises(ValueError, match=r"'ghost'.*'nobody'"):
        installer._route_script_copies(home, tmp_path / "render", SPEC,
                                       _routes(ghost={"profile": "nobody", "script": "relay.py"}))


def test_route_scripts_rejects_traversal(installer, tmp_path):
    """A script path with a `..` component refuses and copies nothing over the profile's config."""
    home = _home(tmp_path)
    profile = home / "profiles" / "orchestrator"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("keep: true\n", encoding="utf-8")
    before = (profile / "config.yaml").read_bytes()
    with pytest.raises(ValueError, match=r"'\.\.'"):
        installer._route_script_copies(home, tmp_path / "render", SPEC,
                                       _routes(esc={"profile": "orchestrator", "script": "../config.yaml"}))
    assert (profile / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("key, value", [
    ("bundle", "/srv/osh-lane;id"), ("bundle", "/srv/osh lane"), ("env_file", "lane.env'"),
    ("shim", "bin/open%hshell"), ("interpreter", "$(id)"), ("members", "../token"),
    ("bundle", "srv/osh-lane"), ("shim", "/bin/openshell"),
])
def test_ssh_lane_rejects_shell_metacharacters(installer, key, value):
    """Every lane value is a plain path of the right kind; anything a shell or ssh would expand refuses."""
    lane = dict(LANE, members=[value]) if key == "members" else dict(LANE, **{key: value})
    with pytest.raises(ValueError, match=f"ssh_lane.{key}"):
        installer._ssh_lane({"ssh_lane": lane})
    assert installer._ssh_lane({"ssh_lane": LANE})["bundle"] == "/srv/osh-lane"


def test_wire_manifest_strict(installer, tmp_path):
    """A manifest line that is not `hermes wire add <role> <role>` refuses, naming the file and line."""
    spec_path = tmp_path / "coworker-types.yaml"
    spec_path.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "wire-manifest.txt"
    manifest.write_text("# ring\nhermes wire add orchestrator triager\n", encoding="utf-8")
    assert installer._spec_wires(SPEC, spec_path) == [["orchestrator", "triager"]]
    manifest.write_text("hermes wire add orchestrator triager\nhermes wire add triager stranger\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"wire-manifest\.txt:2"):
        installer._spec_wires(SPEC, spec_path)
    assert installer._spec_wires({**SPEC, "wires": []}, spec_path) == []


def test_ac20_probe_profile_scope(tmp_path, monkeypatch):
    """The AC-20 probe records a session's critique and plan inside the pass's profile scope, so the gate running
    under the approver's scope finds them, and the launch home's store holds none of them."""
    from gateway.run import _profile_runtime_scope

    root = tmp_path / "fleet-f62d-root"
    approver = root / "profiles" / "approver"
    approver.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    probe = _load(PROBE, "upg_f69sa_ac20_probe")
    stores = _load(STORES, "upg_f69sa_gates_stores")
    seen = {}

    def handle(tool, args, *, task_id, session_id):
        seen.update(sid=session_id, fresh=stores.critique_fresh(session_id, probe.STAGES),
                    plan=stores.has_plan(session_id))
        return json.dumps({"error": "refused"})

    data = probe._dispatch(stores, lambda: _profile_runtime_scope(approver), handle, "approver", "terminal", "true",
                           plan=True)
    assert data == {"error": "refused"}
    assert seen["fresh"] and seen["plan"], seen
    assert not stores.critique_fresh(seen["sid"], probe.STAGES)
    assert not stores.has_plan(seen["sid"])


@pytest.fixture(scope="module")
def detect():
    return _load(PREDICATES, "upg_f69sa_guardrails_predicates").pr_approval_or_merge


@pytest.mark.parametrize("form_id", sorted(FORMS))
def test_detector_joins_line_continuations(detect, form_id):
    """Each AC-20 form, one or more per refused class, is refused as the same class when its words are split over
    backslash-newline continuations."""
    form = FORMS[form_id][1]
    assert detect(form)
    assert detect(form.replace(" ", " \\\n  ")) == detect(form)


@pytest.mark.parametrize("command, refused", [
    ("gh pr review 5 --repo o/r --approve>/dev/null", "gh pr review --approve"),
    ("gh pr review 5 -a</dev/null", "gh pr review --approve"),
    ("gh pr review 5 --approve&", "gh pr review --approve"),
    ("gh pr review 5 --approve=true;echo done", "gh pr review --approve"),
    ("gh pr review 5 --body-file /tmp/review#1.md --approve", "gh pr review --approve"),
    ("gh pr review 5 --repo o/r \\\r\n  --approve", "gh pr review --approve"),
    ("gh pr \\\nmerge 5 --squash", "gh pr merge"),
    ("gh pr review 5 --comment -b ok>/dev/null", None),
    ("gh pr review 5 --approve=false&", None),
])
def test_detector_reads_glued_operators(detect, command, refused):
    """An operator glued to a flag ends its token, and a continuation inside ``gh pr`` still names the verb."""
    assert detect(command) == refused


@pytest.mark.parametrize("source, shown", [
    ("https://bot:s3cr3t-token@example.invalid/o/fork.git", "https://example.invalid/o/fork.git"),
    ("s3cr3t-token@example.invalid:o/fork.git", "example.invalid:o/fork.git"),
])
def test_fork_source_userinfo_never_echoed(installer, tmp_path, monkeypatch, source, shown):
    """The plan line and the refusal name ``--fork-source`` without its userinfo."""
    sealed = tmp_path / "sealed"
    (sealed / "objects").mkdir(parents=True)
    monkeypatch.setattr(installer, "_sealed_source", lambda: str(sealed))
    monkeypatch.setattr(installer, "_fork_mirror_paths", lambda home: (tmp_path / "mirror.git", tmp_path / "gitbin"))
    monkeypatch.setattr(installer, "_real_git", lambda skip: "git")
    monkeypatch.setattr(installer, "_has_commit", lambda *args, **kwargs: False)
    monkeypatch.setattr(installer, "_git_dir", lambda repo: sealed)
    monkeypatch.setattr(installer, "_isolated_git_env", dict)
    monkeypatch.setattr(installer, "_git", lambda git, *args, **kwargs: types.SimpleNamespace(
        returncode=1 if "fetch" in args else 0, stdout=""))
    plan = installer._fork_preflight(tmp_path, "a" * 40, source, plan_only=True)
    with pytest.raises(ValueError) as refused:
        installer._fork_preflight(tmp_path, "a" * 40, source)
    for text in (plan, str(refused.value)):
        assert shown in text
        assert "s3cr3t-token" not in text and "bot:" not in text
