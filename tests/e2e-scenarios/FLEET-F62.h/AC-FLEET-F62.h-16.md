---
ac: AC-FLEET-F62.h-16
kind: live
model: live
base_url: https://inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/fleet-f62-h-bot-a
  - fixtures/fleet-f62-h-bot-b
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-FLEET-F62.h-16: a busy target's delivery is re-driven automatically (live, one gateway)

In the run-3 shape on one gateway, while bot B has no canonical live Bot Chat owner and B's turn lock is held past
`bot_mode.turn_wait_seconds`, bot A's `message_agent` to B gets core's `queued` hand-off ack, A's runner completion
is the tracked `target_busy` → `status: queued` shape, and once B's lock is released the plugin re-drives the
message: exactly one PING row in B's Bot Chat and the ticket `settled`, with no manual relay or resend.

The section below is the ADR's scenario outline (spec r3.3.7.6, ADR `f8718f06`, lines 172-233), copied verbatim. The
tester installs the `fixtures:` list itself; `## Setup` steps after that are post-install only. `<root>` is the
testbed `HERMES_HOME`. Both fixtures already list `nv-delivery-redrive` in `plugins.enabled`; the root config edits
below are the scenario's own.

## Setup check (post-install, before the outline's Setup)

Each installed fixture must be Bot-Mode-managed on its own (`is_bot_mode_managed` is install-wide, so it is not
per-bot proof). Run with the gateway's interpreter; it prints `True True`:

```bash
python3 -c "import hermes_yaml as y; from pathlib import Path; r=Path('<root>'); print(*(isinstance(((y.safe_load((r/'profiles'/b/'profile.yaml').read_text(encoding='utf-8')) or {}).get('ui_meta') or {}).get('hermes-bots'), dict) for b in ('fleet-f62-h-bot-a','fleet-f62-h-bot-b')))"
```

## Scenario outline (verbatim, ADR :152-211)

### AC-FLEET-F62.h-16 (live, model: live, timeout_s: 600)
- **Ships as:** tests/e2e-scenarios/FLEET-F62.h/AC-FLEET-F62.h-16.md
- **Fixtures:** fixtures/fleet-f62-h-bot-a/ and fixtures/fleet-f62-h-bot-b/ (profile dirs with `distribution.yaml`
  at the root, Bot-Mode-managed `profile.yaml`, `plugins.enabled: [nv-delivery-redrive]`). Each fixture's own
  `config.yaml` and the testbed home's root config enable `nv-delivery-redrive` and set the values below. A
  sender's middleware reads its own profile's config, so root-only settings never reach bot A (E9 r3.3.2, builder
  CODE_REVIEW):
  - `bot_mode.turn_wait_seconds: 30`;
  - `plugins.entries.nv-delivery-redrive.settings.orchestrator_profile: fleet-f62-h-bot-a`;
  - `plugins.entries.nv-delivery-redrive.settings.retry_backoff_seconds: 90` (so attempt 2 cannot start while the
    lock is still held; OUTPUT_REVIEW r3 X4);
  - `kanban.dispatch_interval_seconds: 10`.
- **Setup:**
  - One gateway serves both profiles, multiplex, with the kanban dispatcher on (`kanban.dispatch_in_gateway: true`).
  - Live provider block on both profiles: `base_url: https://inference-api.nvidia.com`, api_mode
    `anthropic_messages`, model `aws/anthropic/bedrock-claude-opus-4-8`, key_env `ANTHROPIC_API_KEY` (dummy value;
    the OneCLI proxy injects the real key).
  - B's Bot Chat is NOT open in the desktop app or a TUI, so B has no canonical live owner (E1 F1, reversed for the
    CLI path).
  - Before step 1, hold B's turn lock with core's own lock, from a shell on the gateway host. The markers live in the
    scenario's own dir `<root>/f62h-lock/`, and stale markers are cleared first: `mkdir -p '<root>/f62h-lock' && python3 -c "from pathlib import
    Path; d=Path('<root>/f62h-lock'); (d/'ready').unlink(missing_ok=True); (d/'release').unlink(missing_ok=True)"`. Then start, in the background, `python3 -c "import time; from pathlib import Path; from
    tools.bot_relay import acquire_turn_lock; d = Path('<root>/f62h-lock'); cm = acquire_turn_lock(Path('<root>'),
    'fleet-f62-h-bot-b', timeout_seconds=0); cm.__enter__(); (d / 'ready').touch(); end = time.time() + 300; [time.sleep(0.5)
    for _ in iter(lambda: (d / 'release').exists() or time.time() > end, True)]"`. Run it with the gateway's interpreter
    and working directory; `<root>` is the testbed home.
  - Wait until `<root>/f62h-lock/ready` exists (≤ 30 s; it is new, because the dir was just recreated). The lock is then
    held until step 4 creates `<root>/f62h-lock/release`, 300 s at most. Only core's delivery runner and the relay take this lock
    (`tools/bot_mode_dm.py:408-420`, `tools/bot_relay.py:657-684`, `tui_gateway/methods_bot_relay.py:210`). A plain
    `hermes chat -Q` turn does not, so a long model turn cannot stand in for it.
- **Steps (no model call holds the lock; the live calls are A's two turns and B's one PING turn):**
  1. Check that B has no canonical live owner. `python3 -c "from tools.bot_live_delivery import
     find_canonical_live_owner; print(find_canonical_live_owner('<root>/profiles/fleet-f62-h-bot-b'))"` → expect:
     `None`.
  2. In bot A's Bot Chat, send "Use message_agent to send fleet-f62-h-bot-b exactly this text: 'PING-F62H. Acknowledge in your reply only; do not call message_agent.' Then finish your turn."
     → expect: A's `message_agent` tool row is core's hand-off ack: `status: queued`, a `delivery_id`, a
     `process_id`, and a `redrive` key (`tools/bot_mode_dm.py:743-756`).
  3. Wait `turn_wait_seconds` + 15 s (45 s) while the lock is still held. → expect: A's completion row for that
     process is the tracked busy shape. It parses to `status: queued`, the same `delivery_id`, `reason: target_busy`,
     "automatic re-delivery" and "do NOT resend", and contains none of "retry shortly", "Try again later" or "safe to
     resend" (E1 F4). B's Bot Chat has no "PING-F62H" row yet.
  4. As soon as step 3's completion is observed, release the lock (`touch <root>/f62h-lock/release`). Then wait up to
     `retry_backoff_seconds` + 2 drainer intervals + 60 s. → expect: exactly one user row containing "PING-F62H" in B's
     Bot Chat, written by the plugin's re-drive (the ticket's `attempts` has exactly two rows: n = 1 the core runner,
     n = 2 the re-drive, state `over`). The A→B ticket is `settled` in `<root>/plugin-data/nv-delivery-redrive/data.db`,
     and A's Bot Chat has exactly one plugin delivery notice for that `delivery_id` (the final outcome; the busy
     completion was not final).
- **Pass:** the criterion holds when step 1 prints `None`, step 2 shows the ack with a `redrive` key, step 3 shows
  the tracked busy completion, and step 4 shows exactly one PING row and `settled`, with no manual relay or resend.
  The only bot-directed message is the planned step-2 probe (E2 M3's wording, kept). The lock holder and the probes
  in steps 1 and 3 are the scenario's own shell actions, not messages.
- **Busy live-owner mailbox case (tested separately):** a busy target WITH a live owner is admitted to its mailbox
  before the flock and acks `queued` (`tools/bot_mode_dm.py:663-690`). That ack is not evidence of a CLI re-drive, so
  it is not a step here. It is covered hermetically by AC-8 (B1, follow only) and AC-12 `settle_during_runner`.
- **Budget:** the standard per-scenario cap, `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`, no exception. Planned:
  A's step-2 turn 2 calls, A's turn on the step-3 completion 1-2, B's PING turn 1, A's turn on the reply notice 1-2 =
  5-7 calls, ≤ 14 with retries; estimate $1-3. Hitting the cap is an `ESCALATE`, not a retry with more budget.
- **Evidence:** `evidence.txt` (one outcome line per step); a `python3 -c` sqlite3 query per step over
  `$HERMES_HOME/profiles/fleet-f62-h-bot-b/state.db` (user rows with the sentinel, with ids) and over A's state.db
  (the tool row and the completion row); one over the plugin's `data.db` (ticket + attempts); the gateway log lines
  naming the `delivery_id`. Also list the tool rows of B's PING turn, so a stray B `message_agent` is reported as a
  drive failure, not a plugin one (E16).


## Pass

Step 1 prints `None`, step 2 shows the ack with a `redrive` key, step 3 shows the tracked busy completion, and step 4
shows exactly one PING row and `settled`, with no manual relay or resend.

## Evidence

`evidence.txt` (one outcome line per step) under `scenario-AC-FLEET-F62.h-16/`, plus the `python3 -c` sqlite3
queries named in the outline's **Evidence** bullet (the coworker image has no `sqlite3` CLI), and the gateway log
lines naming the `delivery_id`.
