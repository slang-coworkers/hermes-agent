"""A2A-F21 runaway protection — hermetic proof of the native guardrails a CONFIGURE row turns on.

Node ids join the ADR criteria mechanically: test_ac_a2a_f21_<n> <-> AC-A2A-F21-<n>.
The live criterion AC-A2A-F21-5 has no function here (its artifact is
tests/e2e-scenarios/A2A-F21/AC-A2A-F21-5.md). No network; nothing written under ~/.hermes.
"""

from __future__ import annotations

from pathlib import Path

from agent.tool_guardrails import (
    STALL_GUARD_IDENTICAL_CALL_THRESHOLD,
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    is_stall_guard_repeatable,
)


def test_ac_a2a_f21_1():
    """With hard_stop_enabled: true and a low hard_stop_after.exact_failure, an identical failing tool call is blocked before its next execution; with the stock default false the same sequence is only warned, never blocked."""
    args = {"cmd": "sh -c 'echo boom; exit 1'"}
    fail = '{"error": "boom", "exit_code": 1}'

    configured = ToolCallGuardrailController(
        ToolCallGuardrailConfig.from_mapping(
            {
                "hard_stop_enabled": True,
                "warn_after": {"exact_failure": 2},
                "hard_stop_after": {"exact_failure": 2},
            }
        )
    )
    assert configured.before_call("terminal", args).action == "allow"
    configured.after_call("terminal", args, fail, failed=True)
    assert configured.before_call("terminal", args).action == "allow"
    configured.after_call("terminal", args, fail, failed=True)
    blocked = configured.before_call("terminal", args)
    assert blocked.action == "block", "configured hard_stop_enabled did not circuit-break the runaway"
    assert blocked.code == "repeated_exact_failure_block"

    # Negative control: the stock default (hard_stop_enabled false) must still WARN but
    # never block — asserting the warn defeats a regression that silently drops warnings.
    default = ToolCallGuardrailController(ToolCallGuardrailConfig.from_mapping({}))
    assert default.config.hard_stop_enabled is False
    after_actions = []
    for _ in range(5):
        assert default.before_call("terminal", args).action == "allow"
        after_actions.append(default.after_call("terminal", args, fail, failed=True).action)
    assert default.before_call("terminal", args).action == "allow"
    assert "warn" in after_actions, "warnings must still fire when hard stops are disabled"
    assert "block" not in after_actions and "halt" not in after_actions, (
        "stock default must never circuit-break — config is what turns warn into hard-stop"
    )


def test_ac_a2a_f21_2():
    """The native identical-tool-call breaker appends an observational loop-breaker notice on the 3rd consecutive identical call+result, while a poller-exempt tool and a changed result get no notice."""
    ctrl = ToolCallGuardrailController()
    args = {"target": "peer-b", "message": "ack"}
    result = '{"status": "sent"}'
    assert ctrl.observe_identical_call("message_agent", args, result) is None
    assert ctrl.observe_identical_call("message_agent", args, result) is None
    notice = ctrl.observe_identical_call("message_agent", args, result)
    assert notice is not None and isinstance(notice, str) and notice.strip(), (
        "identical-call breaker did not fire on the 3rd consecutive identical call"
    )
    assert STALL_GUARD_IDENTICAL_CALL_THRESHOLD == 3

    # Negative control A: a poller-exempt tool never gets the loop-breaker notice.
    assert is_stall_guard_repeatable("process") is True
    poller = ToolCallGuardrailController()
    for _ in range(5):
        assert poller.observe_identical_call("process", {"pid": 42}, '{"running": true}') is None

    # Negative control B: a call whose result changes each time is not an identical repeat.
    changing = ToolCallGuardrailController()
    for i in range(5):
        assert changing.observe_identical_call("web_search", {"q": "x"}, f'{{"n": {i}}}') is None


def test_ac_a2a_f21_3(tmp_path, monkeypatch):
    """The kanban.failure_limit circuit-breaker auto-blocks a task once its consecutive-failure count reaches the configured limit, and leaves it un-blocked below the limit."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="a2a-f21 runaway reproducer")
        kb.claim_task(conn, tid)

        # Negative control: one failure of a limit of two does not trip the breaker.
        tripped_1 = kb._record_task_failure(conn, tid, "boom-1", outcome="crashed", failure_limit=2)
        assert tripped_1 is False
        assert kb.get_task(conn, tid).status != "blocked"

        tripped_2 = kb._record_task_failure(conn, tid, "boom-2", outcome="crashed", failure_limit=2)
        assert tripped_2 is True, "circuit-breaker did not trip at the configured failure_limit"
        assert kb.get_task(conn, tid).status == "blocked"


def _blocks(text: str) -> list[str]:
    """Match units: each Markdown table row is its own block; other paragraphs are line-joined.

    Splitting only on blank lines would make a whole table one block, letting terms in
    unrelated rows falsely co-occur — so table rows are separated here.
    """
    blocks: list[str] = []
    for paragraph in text.split("\n\n"):
        lines = [line.strip().lower() for line in paragraph.splitlines() if line.strip()]
        table_rows = [line for line in lines if line.startswith("|")]
        if table_rows:
            blocks.extend(table_rows)
        elif lines:
            blocks.append(" ".join(lines))
    return blocks


def test_ac_a2a_f21_4():
    """The guardrail documentation states the managed-scope-pinned fleet config and maps each NanoClaw runaway behaviour to its honest Hermes landing, including that echo-drop's message-level suppression is not fully native."""
    repo_root = Path(__file__).resolve().parents[2]
    config_md = repo_root / "website" / "docs" / "user-guide" / "configuration.md"
    docker_md = repo_root / "website" / "docs" / "user-guide" / "docker.md"
    assert config_md.exists() and docker_md.exists(), "guardrail doc pages are missing"

    blocks = _blocks(config_md.read_text(encoding="utf-8")) + _blocks(docker_md.read_text(encoding="utf-8"))

    def co_occurs_all(*terms: str) -> bool:
        ts = [t.lower() for t in terms]
        return any(all(t in blk for t in ts) for blk in blocks)

    assert co_occurs_all("managed scope", "tool_loop_guardrails", "kanban.failure_limit"), (
        "the fleet runaway config is not documented as pinned in managed scope"
    )
    assert co_occurs_all("hard_stop_enabled: true", "failure_limit"), (
        "the pinned fleet guardrail config (hard_stop_enabled: true + failure_limit) is not documented together"
    )
    # echo-drop's landing must be stated HONESTLY: the identical-call breaker is only a
    # partial mitigation, so the block must carry the not-shipped/deferred caveat.
    assert co_occurs_all("echo-drop", "identical-call", "deferred") or co_occurs_all(
        "echo-drop", "identical-call", "not shipped"
    ), "echo-drop's landing must state that its message-level suppression is deferred / not shipped"
    mappings = [
        ("bounced-a2a", "cross-gateway"),
        ("runaway card", "cost"),
        ("a2a_max_pingpong_turns", "not rendered"),
    ]
    unmapped = [pair for pair in mappings if not co_occurs_all(*pair)]
    assert not unmapped, f"NanoClaw runaway behaviours not mapped to their landing together: {unmapped}"
