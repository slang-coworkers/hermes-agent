"""Acceptance test for FLEET-F62.c — the Base-NanoClaw coworker fleet spec.

Ships to ``tests/plugins/test_fleet_f62_c_acceptance.py``. One
``test_ac_fleet_f62_c_<n>`` per pytest ``## Acceptance criteria`` row (8), the
mechanical join key for the tester Results rows, the reviewer cross-walk and the
merge gate.

Fail-on-stock asymmetry: the ``coworker`` CLI verb (``nv-coworker-compose``),
``hermes wire`` / ``nv_fleet_gates.edges`` and the committed ``nanoclaw-base``
spec + fixtures + PORT-NOTES are all fork-only. On the stock v2026.8.31 tree the
render subprocess returns non-zero (unknown ``coworker`` command) and the edges
import raises, so every criterion fails; the file passes only once the merged
plugins AND this row's committed content exist. The builder locks the golden
BYTE values (after the orchestrator accepts the [Spec handoff]); the ASSERTIONS
below — counts, structure, edge set, route contract, PORT-NOTES keys — are fixed
by this ADR.

Behavior contract, not a snapshot: no source is read by the test, no network is
used (``github_comment`` is asserted at the CONFIG level, never invoked), nothing
is written under ``~/.hermes`` (tmp_path home + conftest sandbox).
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HOME_TOKEN = "@HERMES_HOME@"  # golden portability: the render-time $HERMES_HOME is substituted on both sides before the byte-diff

# tests/plugins/test_fleet_f62_c_acceptance.py -> repo root is parents[2]
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_SRC = REPO_ROOT / "plugins"
SCENARIO = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c"
SPEC = SCENARIO / "spec" / "openshell" / "coworker-types.yaml"
GOLDEN = SCENARIO / "fixtures" / "openshell"
WIRE_MANIFEST = SCENARIO / "spec" / "openshell" / "wire-manifest.txt"
PORT_NOTES = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-nanoclaw-base-port.md"

FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates")
COWORKERS = ("orchestrator", "triager", "fixer", "reviewer", "approver")
# Per-role bound skills beyond the shared {base-nanoclaw, codex-critique} (ADR §Design).
ROLE_EXTRA = {
    "orchestrator": {"supervise-issues"},
    "triager": {"triage-issue"},
    "fixer": {"implement"},
    "reviewer": {"plan"},
    "approver": set(),
}
SHARED_SKILLS = {"base-nanoclaw", "codex-critique"}
# 6 rendered profiles = 5 coworkers + the non-sandboxed DEFAULT multiplexer.
N_PROFILES = 6
N_POLICIES = 5  # one openshell policy per coworker; DEFAULT has none (OSH-F63 baseline).

# The dispatch's chain-flow arrows, asserted as UNDIRECTED pairs because the
# merged nv-fleet-gates edge store is bidirectional-only (orchestrator msg 8-A).
RING = {
    frozenset(("orchestrator", "triager")),
    frozenset(("triager", "fixer")),
    frozenset(("fixer", "reviewer")),
    frozenset(("reviewer", "approver")),
    frozenset(("approver", "orchestrator")),
}
NON_RING_PAIR = frozenset(("orchestrator", "fixer"))

GITHUB_ROUTES = {
    "github-triager": ("triager", ["issue_comment", "issues"]),
    "github-fixer": ("fixer", ["pull_request", "push"]),
    "github-reviewer": ("reviewer", ["pull_request_review", "pull_request_review_comment"]),
}
# deliver_extra payload templates (webhook _render_prompt {dotted.path} syntax).
DELIVER_REPO = "{repository.full_name}"
PR_NUMBER_TMPL = {  # PR-bearing events carry pull_request.number; issue_comment carries issue.number
    "github-triager": "{issue.number}",
    "github-fixer": "{pull_request.number}",
    "github-reviewer": "{pull_request.number}",
}
_SECRET_RE = re.compile(r"\$\{env:[A-Z][A-Z0-9_]*\}")  # managed-config env SecretRef (config.py:2871-2900)

PORTNOTES_KEYS = (
    "base-nanoclaw",             # mcp__nanoclaw__* transport has no Hermes 1:1
    "ncl",                       # host CLI (tasks/sessions/cost-cap)
    "gate-critique-on-deliver",  # host PostToolUse gate hook
    "gate-chain-routing",        # host PreToolUse gate hook
    "track-critique",            # host critique-tracking hook
    "message",                   # <message> final-response dispatch semantics
    "approver",                  # no base-nanoclaw counterpart
    "bot-identity",              # triage-issue bot identity parameterized
    "buddy",                     # inherited skill disposition
    "explain-diff-html",         # inherited skill disposition
    "f64.b-egress",              # F63-shape egress fallback recorded
    "github_comment",            # deliberate deviation from CH-F52 AC-CH-F52-3
    "base",                      # generic base workflow
    "plan",                      # generic plan workflow
    "implement",                 # generic implement workflow
    "triage-issue",              # generic triage-issue workflow
)

_DESC_MAX = 60  # SKILL_PROMPT_DESC_LIMIT (release skill_utils.py:1182)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")  # release skills_hub.py:214
_MARKETING = ("powerful", "seamless", "cutting-edge", "revolutionary", "amazing", "best-in-class")


def _isolated_home(tmp_path):
    """A HERMES_HOME with the two fleet plugins copied in and an empty bundled
    dir, so only they load (the tests/hermes_cli/test_plugin_api_compat.py shape).
    On the stock tree PLUGINS_SRC lacks these dirs, so nothing is copied and the
    compose subprocess fails — the intended fail-on-stock arm."""
    home = tmp_path / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in FLEET_PLUGINS:
        src = PLUGINS_SRC / key
        if src.is_dir():
            shutil.copytree(src, home / "plugins" / key)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}),
        encoding="utf-8",
    )
    (tmp_path / "empty-bundled").mkdir(exist_ok=True)
    return home


def _render(out, home, *extra):
    """Run ``hermes coworker compose <SPEC> --out <out> [extra]`` isolated."""
    env = dict(os.environ)
    env.update(
        HOME=str(home.parent),
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(home.parent.parent / "empty-bundled"),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose",
         str(SPEC), "--out", str(out), *extra],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _profile_dirs(out):
    """Every rendered profile dir (one config.yaml each), including default —
    EXCLUDING the managed/ fragment, which the composer always emits at
    out/managed/config.yaml (compose.py:132) and is not a profile."""
    return sorted(p.parent for p in out.glob("*/config.yaml") if p.parent.name != "managed")


def _profile_dir(out, role):
    matches = [p.parent for p in out.glob("*/config.yaml") if p.parent.name.endswith(role) or role in p.parent.name]
    assert matches, f"no rendered profile dir for {role!r} under {out}"
    return matches[0]


def _config(profile_dir):
    return yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))


def _tree_files(root):
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def _parse_frontmatter(text):
    """Strict frontmatter parse: the file must OPEN with a --- fence and have a
    matching closing --- on its own line; the block must be a YAML mapping.
    Rejects a stray ``---`` substring in the body (the loose-split defect)."""
    m = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
    assert m, "SKILL.md must open with a --- fenced frontmatter block"
    data = yaml.safe_load(m.group(1))
    assert isinstance(data, dict), "frontmatter must parse to a mapping"
    return data


def test_ac_fleet_f62_c_1(tmp_path):
    """Renders exactly 6 profile dirs + 5 openshell policies, deterministically
    and byte-identical to the committed golden."""
    home = _isolated_home(tmp_path)
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    r1 = _render(out1, home)
    r2 = _render(out2, home)
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr

    dirs = _profile_dirs(out1)
    assert len(dirs) == N_PROFILES, f"expected {N_PROFILES} profiles, got {[d.name for d in dirs]}"
    for role in COWORKERS:
        _profile_dir(out1, role)
    default_dir = _profile_dir(out1, "default")
    policies = sorted(out1.glob("*/policy-*.yaml"))
    assert len(policies) == N_POLICIES, f"expected {N_POLICIES} policies, got {[p.name for p in policies]}"
    assert not list(default_dir.glob("policy-*.yaml")), "DEFAULT must be unsandboxed (no policy)"

    assert _tree_files(out1) == _tree_files(out2), "render file set is non-deterministic"
    for rel in _tree_files(out1):
        assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes(), f"non-deterministic bytes: {rel}"

    # Byte-lock each rendered profile subtree + managed/ against the committed
    # golden, home-tokenized so the lock is portable across machines.
    for sub in [d.name for d in dirs] + ["managed"]:
        rendered, golden = out1 / sub, GOLDEN / sub
        assert golden.is_dir(), f"golden dir missing for {sub} (never silently skip a byte-lock)"
        assert _tree_files(rendered) == _tree_files(golden), f"file set differs from golden under {sub}"
        for rel in _tree_files(rendered):
            got = (rendered / rel).read_bytes().replace(str(home).encode(), HOME_TOKEN.encode())
            assert got == (golden / rel).read_bytes(), f"golden byte mismatch (home-tokenized): {sub}/{rel}"


def test_ac_fleet_f62_c_2(tmp_path):
    """The 5 coworker distributions install cleanly; DEFAULT validates + installs
    under a non-reserved probe name; `--name default` is rejected."""
    home = _isolated_home(tmp_path)
    out = tmp_path / "out"
    assert _render(out, home).returncode == 0

    def _install(dist, name, target_home):
        env = dict(os.environ)
        env.update(HOME=str(target_home.parent), HERMES_HOME=str(target_home))
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "profile", "install",
             str(dist), "--name", name, "-y"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        )

    for role in COWORKERS:
        dist = _profile_dir(out, role)
        rendered_skills = {p.parent.name for p in dist.glob("skills/*/SKILL.md")}
        assert rendered_skills, f"{role}: rendered dist has no skills"
        thome = tmp_path / f"inst-{role}" / ".hermes"
        thome.mkdir(parents=True)
        proc = _install(dist, role, thome)
        assert proc.returncode == 0, f"{role} install failed: {proc.stderr}"
        installed_skills = {p.parent.name for p in (thome / "profiles" / role / "skills").glob("*/SKILL.md")}
        assert installed_skills == rendered_skills, \
            f"{role}: installed skills {installed_skills} != rendered {rendered_skills}"

    default_dist = _profile_dir(out, "default")
    # distribution.yaml is schema-valid (only `name` is required).
    manifest = yaml.safe_load((default_dist / "distribution.yaml").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict) and manifest.get("name"), "DEFAULT distribution.yaml needs a name"
    # `default` is a reserved install name (profile_distribution.py:534).
    rej_home = tmp_path / "rej" / ".hermes"; rej_home.mkdir(parents=True)
    assert _install(default_dist, "default", rej_home).returncode != 0, "--name default must be rejected"
    # ...but its payload installs under a non-reserved probe name.
    probe_home = tmp_path / "probe" / ".hermes"; probe_home.mkdir(parents=True)
    assert _install(default_dist, "nanoclaw-base-default-probe", probe_home).returncode == 0


def test_ac_fleet_f62_c_3(tmp_path):
    """Every converted skill is well-formed: strict frontmatter dict, name rule,
    description HARDLINE (<=60/one-sentence/period/no-marketing), scan_skill clean."""
    from tools import skills_guard  # fork/release surface; ImportError on a broken tree

    # Validate the CONVERTED SOURCE deliverables directly (skills/ + workflows/),
    # so the unbound splice-parent `base` workflow is covered too — the composer
    # renders only bound workflows (compose.py:2845), so a rendered-only check
    # would silently skip base.
    spec_root = SPEC.parent
    skill_files = (sorted((spec_root / "skills").glob("*/SKILL.md"))
                   + sorted((spec_root / "workflows").glob("*/SKILL.md")))
    assert skill_files, "no converted skill/workflow SKILL.md sources to validate"
    names = {p.parent.name for p in skill_files}
    expected = {"base-nanoclaw", "codex-critique", "supervise-issues",
                "base", "plan", "implement", "triage-issue"}
    assert expected <= names, f"converted skills/workflows missing: {expected - names}"

    # Validate rendered skills too — a malformed RENDER (splice output) must not
    # pass because only the source was checked. `base` is the unbound parent, so
    # it is validated as a source above but not expected in the render.
    home = _isolated_home(tmp_path)
    out = tmp_path / "out"
    result = _render(out, home)
    assert result.returncode == 0, result.stderr
    rendered_files = sorted(out.glob("*/skills/*/SKILL.md"))
    assert expected - {"base"} <= {p.parent.name for p in rendered_files}, \
        "converted skills/workflows missing from the render"

    for role, extra in ROLE_EXTRA.items():
        role_dir = _profile_dir(out, role)
        role_skills = {p.parent.name for p in role_dir.glob("skills/*/SKILL.md")}
        want = SHARED_SKILLS | extra
        assert want <= role_skills, f"{role}: rendered skills {role_skills} missing {want - role_skills}"

    for sk in skill_files + rendered_files:
        fm = _parse_frontmatter(sk.read_text(encoding="utf-8"))
        name = fm.get("name", "")
        desc = str(fm.get("description", "")).strip()
        assert _NAME_RE.fullmatch(name), f"{sk}: name {name!r} violates ^[a-z][a-z0-9_-]*$"
        assert 0 < len(desc) <= _DESC_MAX, f"{sk}: description len {len(desc)} > {_DESC_MAX}"
        assert desc.endswith("."), f"{sk}: description must end with a period"
        assert ". " not in desc, f"{sk}: description must be one sentence (interior '. ' found)"
        assert not any(w in desc.lower() for w in _MARKETING), f"{sk}: marketing word in description"
        result = skills_guard.scan_skill(sk.parent)
        findings = getattr(result, "findings", result)
        assert not findings, f"{sk}: skills_guard.scan_skill findings: {findings}"


def test_ac_fleet_f62_c_4(tmp_path):
    """Applying the wire manifest yields exactly the 5 undirected ring pairs and
    no extra edge (hermes wire list == list_edges over the fleet edge store)."""
    import json

    commands = []
    for raw in WIRE_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = shlex.split(line)
        assert parts[:3] == ["hermes", "wire", "add"] and len(parts) == 5, \
            f"manifest line must be `hermes wire add <a> <b>`, got {line!r}"
        commands.append(parts)
    assert len(commands) == 5, f"expected 5 wire-add commands, got {len(commands)}"
    assert {frozenset((c[3], c[4])) for c in commands} == RING, "manifest pairs != ring"

    home = _isolated_home(tmp_path)
    db = str(tmp_path / "edges.db")
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("plugins", {}).setdefault("entries", {}).setdefault(
        "nv-fleet-gates", {}).setdefault("settings", {})["edges_db_path"] = db
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    env = dict(os.environ)
    env.update(HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(home.parent.parent / "empty-bundled"),
               HERMES_ENABLE_PROJECT_PLUGINS="0")

    def _wire(*args):
        return subprocess.run([sys.executable, "-m", "hermes_cli.main", "wire", *args],
                              capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))

    for _hermes, _wire_v, _add, a, b in commands:
        proc = _wire("add", a, b)
        assert proc.returncode == 0, f"hermes wire add {a} {b} failed: {proc.stderr}"

    lst = _wire("list")
    assert lst.returncode == 0, f"hermes wire list failed: {lst.stderr}"
    rows = json.loads(lst.stdout)["edges"]
    directed = {(r[0], r[1]) for r in rows}
    expected_directed = {(a, b) for p in RING for a, b in (tuple(p), tuple(p)[::-1])}
    assert directed == expected_directed, f"hermes wire list {directed} != both directions of the ring"
    assert tuple(NON_RING_PAIR) not in directed and tuple(NON_RING_PAIR)[::-1] not in directed, \
        f"unexpected non-ring edge present: {NON_RING_PAIR}"


def test_ac_fleet_f62_c_5(tmp_path):
    """--provision-dry-run lists exactly 5 `sandbox create` (the 5 coworkers;
    DEFAULT absent), deterministically across two runs."""
    home = _isolated_home(tmp_path)
    r1 = _render(tmp_path / "o1", home, "--provision-dry-run")
    r2 = _render(tmp_path / "o2", home, "--provision-dry-run")
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert r1.stdout == r2.stdout, "provision dry-run is non-deterministic"

    create_lines = [ln for ln in r1.stdout.splitlines() if "sandbox create" in ln]
    assert len(create_lines) == len(COWORKERS), \
        f"expected {len(COWORKERS)} create lines, got {create_lines}"
    names = set()
    for ln in create_lines:
        m = re.search(r"--name\s+(\S+)", ln)
        assert m, f"create line without --name: {ln!r}"
        names.add(m.group(1))
    assert names == {f"nanoclaw-base-{role}" for role in COWORKERS}, \
        f"provision create names {names} != the 5 coworker sandboxes"
    assert "nanoclaw-base-default" not in names, "DEFAULT must not be provisioned a sandbox"

    # Byte-lock the plan against the committed golden (home-independent).
    prov_golden = GOLDEN / "provision.dry-run.txt"
    assert prov_golden.is_file(), f"provision golden missing at {prov_golden}"
    assert r1.stdout.encode("utf-8") == prov_golden.read_bytes(), \
        "provision plan differs from the committed golden"


def test_ac_fleet_f62_c_6(tmp_path):
    """Negative control: renderer present but the fleet spec dir absent ->
    non-zero exit and no distribution written."""
    home = _isolated_home(tmp_path)
    # Precondition: the renderer IS present (a positive render succeeds). This
    # arm fails on the stock tree, where `coworker` is not a command.
    assert _render(tmp_path / "positive", home).returncode == 0, \
        "precondition: the renderer must be present and render the committed spec"

    out = tmp_path / "out"
    env = dict(os.environ)
    env.update(
        HOME=str(home.parent), HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(home.parent.parent / "empty-bundled"),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
    )
    missing = tmp_path / "no-such-spec" / "coworker-types.yaml"
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose",
         str(missing), "--out", str(out)],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0, "compose must fail when the fleet spec dir is absent"
    assert not out.exists(), "compose must leave no output tree for a missing spec"


def test_ac_fleet_f62_c_7(tmp_path):
    """PORT-NOTES.md records every dropped/changed skill behaviour — one
    non-empty entry per expected key."""
    assert PORT_NOTES.is_file(), f"PORT-NOTES missing at {PORT_NOTES}"
    text = PORT_NOTES.read_text(encoding="utf-8")

    def _cells(line):
        return [c.strip() for c in line.strip().strip("|").split("|")]

    rows = [ln for ln in text.splitlines() if ln.strip().startswith("|") and ln.count("|") >= 2]
    header_i = next((i for i, ln in enumerate(rows)
                     if any("source" in c.lower() for c in _cells(ln))
                     and any("behav" in c.lower() for c in _cells(ln))), None)
    assert header_i is not None, "PORT-NOTES needs a table with `source` and `changed behaviour` columns"
    header = [c.lower() for c in _cells(rows[header_i])]
    src_i = next(i for i, c in enumerate(header) if "source" in c)
    beh_i = next(i for i, c in enumerate(header) if "behav" in c)
    data = [_cells(ln) for ln in rows[header_i + 1:] if not set(_cells(ln)[0]) <= set("-: ")]

    def _src_tokens(cell):
        # token-equality (not substring): `base` must not match `base-nanoclaw`.
        return set(t for t in re.split(r"[\s,()/]+", cell.lower()) if t)

    for key in PORTNOTES_KEYS:
        matched = [r for r in data
                   if len(r) > max(src_i, beh_i) and key.lower() in _src_tokens(r[src_i])]
        assert matched, f"PORT-NOTES has no `source` row with the exact token {key!r}"
        behaviour = matched[0][beh_i].strip()
        assert len(behaviour) >= 8, f"PORT-NOTES row for {key!r} has a non-substantive behaviour cell"


def test_ac_fleet_f62_c_8(tmp_path):
    """The rendered DEFAULT config carries the `github` ingress byte-stable: 3
    profile-bound routes, deliver github_comment + deliver_extra, a managed-config
    HMAC secret placeholder ref, orchestrator/approver route-free, coworkers
    webhook-disabled."""
    home = _isolated_home(tmp_path)
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    assert _render(out1, home).returncode == 0
    assert _render(out2, home).returncode == 0
    d1, d2 = _profile_dir(out1, "default"), _profile_dir(out2, "default")
    assert (d1 / "config.yaml").read_bytes() == (d2 / "config.yaml").read_bytes(), \
        "DEFAULT config.yaml render is non-deterministic"

    default_cfg = _config(d1)
    routes = (((default_cfg.get("platforms") or {}).get("webhook") or {}).get("extra") or {}).get("routes") or {}
    assert set(routes) == set(GITHUB_ROUTES), f"DEFAULT routes {set(routes)} != {set(GITHUB_ROUTES)}"

    for name, (profile, events) in GITHUB_ROUTES.items():
        rc = routes[name]
        assert rc.get("profile") == profile, f"{name}: profile {rc.get('profile')!r} != {profile!r}"
        assert sorted(rc.get("events", [])) == events, f"{name}: events {rc.get('events')} != {events}"
        assert rc.get("deliver") == "github_comment", f"{name}: deliver != github_comment"
        de = rc.get("deliver_extra") or {}
        assert de.get("repo") == DELIVER_REPO, f"{name}: deliver_extra.repo {de.get('repo')!r} != {DELIVER_REPO!r}"
        assert de.get("pr_number") == PR_NUMBER_TMPL[name], \
            f"{name}: deliver_extra.pr_number {de.get('pr_number')!r} != {PR_NUMBER_TMPL[name]!r}"
        secret = str(rc.get("secret", ""))
        assert _SECRET_RE.fullmatch(secret), \
            f"{name}: secret {secret!r} must be an unresolved ${{env:NAME}} managed-config placeholder"
        rc_keys = {k.lower() for k in rc}
        assert "app_id" not in rc_keys and "app-id" not in rc_keys, f"{name}: no App id"
        assert {"host", "port", "url", "health_probe", "health-probe"}.isdisjoint(rc_keys), \
            f"{name}: no live host/port/url/health-probe in the route"
        assert "http://" not in yaml.safe_dump(rc) and "https://" not in yaml.safe_dump(rc), f"{name}: no live URL"

    extra = ((default_cfg.get("platforms") or {}).get("webhook") or {}).get("extra") or {}
    assert {"host", "port", "url", "health_probe", "health-probe"}.isdisjoint({k.lower() for k in extra}), \
        "webhook extra must carry no live host/port/url/health-probe"
    assert {p for (p, _e) in GITHUB_ROUTES.values()} == {"triager", "fixer", "reviewer"}
    for role in COWORKERS:
        wh = (_config(_profile_dir(out1, role)).get("platforms") or {}).get("webhook") or {}
        assert not (wh.get("extra") or {}).get("routes"), f"{role} must carry no webhook routes"
