"""ISO-F17 own-diff doc-contract negative control.

ADOPT rows ship no runtime code, so the 11 AC-ISO-F17-<n> acceptance tests are
green-on-stock conformance proofs (each with its own differential control) and
carry NO fail-before/pass-after asymmetry. That asymmetry — the merge gate's
fails-on-base requirement — lives here: this test locates the new
``### Host sweep & restart recovery`` runbook section, which is ABSENT on the
base branch (so this test FAILS on base) and PRESENT on head (so it PASSES), and
asserts the section actually carries ISO-F17's operational guidance and the
NanoClaw-sweep-item -> Hermes-owner mapping — not just token presence file-wide.

All assertions are scoped to the new section (``_section`` below): the target
page already documents the multiplexer elsewhere, so a file-wide search would
pass on base for tokens like ``multiplex_profiles``. Section-scoping is what
makes this a real negative control rather than a snapshot of the whole page.
"""

from pathlib import Path

# tests/hermes_cli/test_iso_f17_doc_contract.py -> parents[2] is the repo root
DOC_PAGE = (
    Path(__file__).resolve().parents[2]
    / "website" / "docs" / "user-guide" / "multi-profile-gateways.md"
)

SECTION_HEADING = "### Host sweep & restart recovery"


def _section(text: str) -> str:
    """Return only the ``### Host sweep & restart recovery`` section body.

    From the section heading up to (not including) the next ``##``/``###``
    heading, or end of file. Empty string when the heading is absent — which is
    the base-branch case that makes this test fail on base.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == SECTION_HEADING:
            start = i
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## ") or line.startswith("### "):
            break
        body.append(line)
    return "\n".join(body)


def test_iso_f17_doc_contract():
    assert DOC_PAGE.is_file(), f"ISO-F17 target doc page missing (expected {DOC_PAGE})"
    text = DOC_PAGE.read_text(encoding="utf-8")

    assert SECTION_HEADING in text, (
        f"runbook section {SECTION_HEADING!r} is absent — this is the fails-on-base "
        "state; the head must add it"
    )
    sec = _section(text)
    assert sec.strip(), "the runbook section exists but is empty"
    low = sec.lower()

    # (1) Fleet supervision topology: one multiplex gateway + allowlisted roster.
    for tok in ("multiplex_profiles", "multiplex_profile_allowlist"):
        assert tok in sec, f"section must document the multiplex config key {tok!r}"

    # (2) container_boot reconcile by desired_state, non-mutating, coworkers non-running.
    for tok in ("container_boot", "GATEWAY_MULTIPLEX_PROFILES", "desired_state"):
        assert tok in sec, f"section must name the boot-reconcile seam {tok!r}"
    assert "non-running" in low, (
        "section must state coworker profiles are kept at a non-running desired state"
    )
    assert ("does not mutate" in low) or ("without mutating" in low), (
        "section must state boot reconcile does not mutate persisted desired_state"
    )

    # (3) Restart/stop exit-code contract + watchdog (systemd) and KeepAlive (launchd).
    for tok in (
        "Restart=always",
        "RestartForceExitStatus=75",
        "RestartPreventExitStatus=78",
        "Type=notify",
        "WatchdogSec",
        "shutdown_watchdog",
        "KeepAlive",
    ):
        assert tok in sec, f"section must document the supervision seam {tok!r}"
    # launchd relaunches on any exit — no exit-78 permanent stop.
    assert "launchd" in low and "unconditional" in low, (
        "section must record launchd KeepAlive is unconditional (no exit-78 stop)"
    )

    # (4) resume_pending, turn leases, kanban reclaim guards, docker reaper.
    for tok in (
        "resume_pending",
        "turn lease",
        "release_stale_claims",
        "check_respawn_guard",
        "reap_orphan_containers",
    ):
        assert tok in sec, f"section must name the sweep/recovery seam {tok!r}"

    # (5) NanoClaw sweep-item -> Hermes-owner mapping (owning requirement rows).
    for row in ("ISO-F11", "SCHED-F32", "SCHED-F33", "ISO-F17"):
        assert row in sec, f"sweep-owner mapping must name the owning row {row!r}"
    for item in ("processing_ack", "delivery", "due", "recurrence", "crash-loop backoff"):
        assert item in low, f"sweep-owner mapping must name the NanoClaw item {item!r}"

    # (6) The fixed 0/0/10/30/120/300/900 backoff array is replaced by supervisor
    #     restart + the start-storm breaker's exponential backoff + the respawn guard.
    assert "0/0/10/30/120/300/900" in sec, (
        "section must record the fixed NanoClaw backoff array it replaces"
    )
    assert "exponential backoff" in low, "section must name dynamic exponential backoff"
    assert "respawn guard" in low, "section must name the respawn guard"

    # (7) The `gateway start` behaviour stated precisely: foreground `gateway run`
    #     refuses with exit 78; supervised `gateway start` returns after
    #     s6-svc -u, then the spawned child exits 78 and the supervisor permanently
    #     stops the slot. Never the unqualified "the start command returns non-zero".
    assert "gateway run" in sec and "78" in sec, (
        "section must state foreground `gateway run` refuses with exit 78"
    )
    assert "gateway start" in sec and "s6-svc -u" in sec, (
        "section must state supervised `gateway start` dispatches s6-svc -u and returns"
    )
    assert "permanently stop" in low, (
        "section must state the supervisor permanently stops the slot (exit 78)"
    )
    assert "the start command returns non-zero" not in low, (
        "section must NOT use the imprecise 'the start command returns non-zero' phrasing "
        "— `gateway start` returns 0 in the s6 path; the spawned child exits 78"
    )

    # (8) An operator recovery drill (kill / restart / health / resume) that uses the
    #     DEFAULT profile's real service identifiers — no `-default` suffix (that form is
    #     the s6/Docker slot, not the systemd unit or launchd label).
    assert "hermes doctor" in sec, "section must give a health-check step"
    assert ("systemctl" in sec) or ("launchctl" in sec), (
        "section must give the OS-supervisor restart step"
    )
    assert "hermes-gateway.service" in sec and "ai.hermes.gateway" in sec, (
        "the drill must name the default-profile systemd unit and launchd label"
    )
    assert "hermes-gateway-default.service" not in sec, (
        "the systemd unit has no -default suffix (that is the s6/Docker slot form)"
    )
    assert "ai.hermes.gateway-default" not in sec, (
        "the launchd label has no -default suffix"
    )
    assert "returns 0" in sec, (
        "section must state supervised `gateway start` returns 0 (the child exits 78)"
    )

    # (9) The acceptance-criterion ids — the join key with the tester/reviewer/merge gate.
    for n in range(1, 12):
        assert f"AC-ISO-F17-{n}" in sec, f"section must carry acceptance id AC-ISO-F17-{n}"
