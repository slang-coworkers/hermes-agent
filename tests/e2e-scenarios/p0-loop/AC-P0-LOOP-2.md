---
ac: AC-P0-LOOP-2
kind: ui
model: stub
fixtures:
  - fixtures/p0-loop-hello
timeout_s: 300
---

# AC-P0-LOOP-2 — `/hello` in the dashboard names the profile

Typing `/hello` in the `p0-loop-hello` profile's dashboard chat shows the
plugin's greeting `Hello from p0-loop-hello!` as the command output. The
`/hello` slash command is handled by the plugin over the PTY-embedded TUI
(`ui-tui/src/app/createSlashHandler.ts:154-168` sends `slash.exec`;
`tui_gateway/methods_tools.py:1260-1279` runs the plugin handler and returns
`{"output": …}`), so the greeting is a deterministic command output — the stub
model is never called for this path.

## Setup

The tester installs the `fixtures:` list (`hermes profile install
tests/e2e-scenarios/p0-loop/fixtures/p0-loop-hello --name p0-loop-hello -y`)
before this section runs; do not re-install it here. After install, with
`$HERMES_HOME` = the shared testbed home:

1. The empty `HERMES_BUNDLED_PLUGINS` dir means each profile owns its plugins,
   and `hermes profile install` does not create a `plugins/` dir, so make it
   and copy the plugin in from the worktree:
   `mkdir -p "$HERMES_HOME/profiles/p0-loop-hello/plugins" && cp -r "$WT/plugins/hello" "$HERMES_HOME/profiles/p0-loop-hello/plugins/"`.
   The installed `config.yaml` already sets `plugins.enabled: [hello]`.
2. Write the stub key post-install (`.env` is user-owned and stripped on
   install): `printf 'MOCK_API_KEY=e2e-mock-key\n' >> "$HERMES_HOME/profiles/p0-loop-hello/.env"`.
   The provider block only exists so the profile is not stuck on the
   provider-onboarding overlay; `/hello` never calls the model, so the stub URL
   need not be reachable.
3. Launch the dashboard scoped to the profile with the global `-p` flag
   (`-p` selects the profile; `--isolated` scopes the server but does not select
   a profile — `hermes_cli/subcommands/dashboard.py:42-60,107-125`):
   ```bash
   ( source $TB/harness.env && cd $WT
     export HERMES_WEB_DIST=<prebuilt SPA dist>
     hermes -p p0-loop-hello dashboard --host 127.0.0.1 --port 9119 \
       --no-open --isolated --skip-build > $ART/scenario-AC-P0-LOOP-2/dashboard.log 2>&1 & )
   timeout 300 bash -c "until grep -qE 'HERMES_DASHBOARD_READY port=|Hermes Web UI' \
     $ART/scenario-AC-P0-LOOP-2/dashboard.log; do sleep 2; done"
   ```

## Steps

The dashboard SPA opens the **default** profile's chat regardless of
`-p p0-loop-hello … --isolated` (`--isolated` scopes the server, not the SPA's
active chat); the launched profile is a **non-selected** combobox option
labelled `this dashboard (p0-loop-hello)`. So the launched profile must be
selected in the UI before `/hello` is typed, or the greeting names `default`
(the round-1 AC-2 defect).

1. In the dashboard (`agent-browser open "http://127.0.0.1:9119/chat"`,
   `agent-browser wait --load networkidle`, `agent-browser snapshot -i`), open
   the profile combobox and select the launched profile `p0-loop-hello` (the
   option labelled `this dashboard (p0-loop-hello)`, by `@ref`), then re-snapshot
   (the page rerenders, so earlier refs are dead)
   → expect: the composer prompt is **prefixed with the profile name**
   `p0-loop-hello ` (a named profile renders `<profile> <glyph>` —
   `ui-tui/src/lib/prompt.ts:31-33`; the glyph is skin/termux-configurable,
   default `❯`, so assert the `p0-loop-hello ` **prefix**, NOT a specific glyph).
   Until selected, the SPA shows the default profile's chat (no profile prefix);
   screenshot `step-1.png`.
2. Fill the composer with `/hello` and submit
   (`agent-browser fill @e<composer> "/hello"`, `agent-browser press Enter`),
   then `timeout 300 agent-browser wait --text "Hello from p0-loop-hello!"`
   → expect: the visible command **output** `Hello from p0-loop-hello!` appears
   in the transcript. The Ink client sends `slash.exec` and renders `r.output`
   via `sys()` (`ui-tui/src/app/createSlashHandler.ts:154-168`); Python runs the
   plugin handler and returns `{"output": …}`
   (`tui_gateway/methods_tools.py:1260-1279`). Assert the visible command output
   text — not an assistant-role model bubble and not a persisted model turn;
   screenshot `step-2.png`.

## Pass

With `p0-loop-hello` selected (composer prompt prefixed `p0-loop-hello `),
typing `/hello` visibly shows `Hello from p0-loop-hello!` as the `/hello`
command output.

## Evidence

- `step-1.png` — `p0-loop-hello` selected in the combobox; composer prompt prefixed `p0-loop-hello `.
- `step-2.png` — `Hello from p0-loop-hello!` visible as the `/hello` output.

The observable is a rendered string, so the screenshot is the proof; no
`state.db` query is required for AC-P0-LOOP-2.
