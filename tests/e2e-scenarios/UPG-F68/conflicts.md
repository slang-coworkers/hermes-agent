# UPG-F68 per-file conflict list — resolved hunks against both sides

Item-1 deliverable (ADR §Merge gate 3, §Design D1/D3; Orchestrator msg 8; architect msgs 42/46, E3.2), at the selected
tag v0.22.1. Measured with git 2.39.5: `git merge-tree --write-tree --name-only --no-messages 297552a9 a2afed6dfa` (merge-base
`f97608f178`) conflicts in exactly the six files below; the `-x` picks are measured in item 3. Each section quotes the
upstream side @tag (`297552a9d6`), the fork side (@H5 `a2afed6dfa`, or @pick), and the resolved hunk as committed (@C1 `c94d27390c`).
The same three locators are the `locators` column of `path_inventory.tsv`, and `replay_check.py` resolves each one.

## 1. `gateway/kanban_watchers.py` `_kanban_sub_op` (core, CORE-CHANGE A1)

Exactly the union (msg 8): upstream `387c78c8`'s `_kb` import, comment and `pin_first_board_resolution()` wrap, with
GOV-F25's `-> Any` and `return getattr(_kbn, op)(…)`. The C1 body is the tag body with those two lines changed; the
A1 equation in `replay_check.py` (d) proves it over the whole function.

**upstream @tag** `gateway/kanban_watchers.py:123-140`

```
    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> None:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        # Cursor writes are machine flow: the sub's board slug must resolve
        # through the env pin on a dispatcher-pinned box (same as the notifier
        # tick), or advance/rewind land on a per-slug DB the notifier never
        # reads and cursors silently reset.
        with _kb.pin_first_board_resolution():
            conn = _kbc.connect(board=board)
            try:
                getattr(_kbn, op)(
                    conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "", **extra,
                )
            finally:
                conn.close()
```

**fork @H5** `gateway/kanban_watchers.py:135-146`

```
    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> Any:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        conn = _kbc.connect(board=board)
        try:
            return getattr(_kbn, op)(
                conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "", **extra,
            )
        finally:
            conn.close()
```

**resolved @C1** `gateway/kanban_watchers.py:135-152`

```
    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> Any:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        # Cursor writes are machine flow: the sub's board slug must resolve
        # through the env pin on a dispatcher-pinned box (same as the notifier
        # tick), or advance/rewind land on a per-slug DB the notifier never
        # reads and cursors silently reset.
        with _kb.pin_first_board_resolution():
            conn = _kbc.connect(board=board)
            try:
                return getattr(_kbn, op)(
                    conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or "", **extra,
                )
            finally:
                conn.close()
```

## 2. `gateway/platforms/api_server_openai_routes.py` (core, CORE-CHANGE A2 + an adjacent insert)

Two conflicts at v0.22.1 (architect msg 46).

**(i) `_run_idempotent` signature (A2).** Upstream's PEP 585 line plus GOV-F25.b's `require_persist: bool = False`.
The function's other GOV-F25.b lines merged cleanly and pass the per-scope replay (c).

**upstream @tag** `gateway/platforms/api_server_openai_routes.py:1013-1015`

```
    async def _run_idempotent(
        self, request: web.Request, body: dict[str, Any], compute, *,
        log_label: str, fingerprint_keys: list[str], route: str) -> tuple:
```

**fork @H5** `gateway/platforms/api_server_openai_routes.py:803-805`

```
    async def _run_idempotent(
        self, request: "web.Request", body: Dict[str, Any], compute, *,
        log_label: str, fingerprint_keys: List[str], route: str, require_persist: bool = False) -> tuple:
```

**resolved @C1** `gateway/platforms/api_server_openai_routes.py:1044-1046`

```
    async def _run_idempotent(
        self, request: web.Request, body: dict[str, Any], compute, *,
        log_label: str, fingerprint_keys: list[str], route: str, require_persist: bool = False) -> tuple:
```

**(ii) Adjacent insert at module level.** Upstream added `_parse_chat_messages`, `_parse_responses_input` and
`_parse_conversation_history` where H5 had added GOV-F25.b's `_cache_if_persisted`. Neither edits the other; both are kept,
upstream's first, two blank lines between. Textual only: whether the session authority's rebuilt turn result still
carries `turn_persisted` at v0.22.1 is E3 reading (c), with the user (architect msg 44).

- upstream @tag `gateway/platforms/api_server_openai_routes.py:660-726` (the three `_parse_*` helpers)
- fork @H5 `gateway/platforms/api_server_openai_routes.py:524-534` (`_cache_if_persisted`)
- resolved @C1 `gateway/platforms/api_server_openai_routes.py:660-737` (the helpers `:660-726`, then `_cache_if_persisted` `:728-737`)

## 3. `tests/gateway/test_plugin_message_injection.py` (in-surface)

Upstream appended `test_under_a_session_authority_injection_enters_the_durable_fifo` at the end of the file; H5 appended
the W2 durable cross-profile delivery block (+742, `f97608f178..a2afed6dfa`) at the same anchor. Resolution: both,
upstream's test first, then all of H5's block verbatim (architect msg 46).

- upstream @tag `tests/gateway/test_plugin_message_injection.py:459-484` (the session-authority test)
- fork @H5 `tests/gateway/test_plugin_message_injection.py:458-1195` (the W2 block)
- resolved @C1 `tests/gateway/test_plugin_message_injection.py:459-1225` (upstream's test `:459-484`, H5's block `:488-1225`)

## 4. `tests/hermes_cli/test_config_read_guard.py` allowlist (in-surface)

Both entries with their comments, upstream's first.

**upstream @tag** `tests/hermes_cli/test_config_read_guard.py:50-53`

```
    # _early_interface_from_config(): the pre-argparse TUI decision runs before
    # hermes_cli.config is importable; it reads one display key best-effort and
    # defaults to the classic REPL on any error.
    "hermes_cli/main.py",
```

**fork @H5** `tests/hermes_cli/test_config_read_guard.py:50-56`

```
    # OSH-F64 fleet installer: raw read-diff-write-back of config.yaml at explicit sandbox
    # paths (the sanctioned raw-read use — see read_user_config_raw's docstring, "ONLY legal
    # for write-back round-trips"). It cannot route through hermes_cli.config's reader
    # because importing that module runs ensure_hermes_home() at import time (config.py),
    # scaffolding HERMES_HOME; the installer's --dry-run planner must NOT mutate the target
    # home (AC-OSH-F64-1), so it stays import-light and reads raw here.
    "plugins/nv-coworker-compose/openshell/installer.py",
```

**resolved @C1** `tests/hermes_cli/test_config_read_guard.py:50-60`

```
    # _early_interface_from_config(): the pre-argparse TUI decision runs before
    # hermes_cli.config is importable; it reads one display key best-effort and
    # defaults to the classic REPL on any error.
    "hermes_cli/main.py",
    # OSH-F64 fleet installer: raw read-diff-write-back of config.yaml at explicit sandbox
    # paths (the sanctioned raw-read use — see read_user_config_raw's docstring, "ONLY legal
    # for write-back round-trips"). It cannot route through hermes_cli.config's reader
    # because importing that module runs ensure_hermes_home() at import time (config.py),
    # scaffolding HERMES_HOME; the installer's --dry-run planner must NOT mutate the target
    # home (AC-OSH-F64-1), so it stays import-light and reads raw here.
    "plugins/nv-coworker-compose/openshell/installer.py",
```

## 5. `tests/hermes_cli/test_kanban_notify.py` (in-surface)

Upstream deleted 34 blank lines only (`git diff f97608f178 297552a9` has no non-blank change in this file); H5 appended the
GOV-F25 UA-6 block (+217, `f97608f178..a2afed6dfa`). Resolution: the tag file, then H5's block verbatim.

- upstream @tag `tests/hermes_cli/test_kanban_notify.py:1-1180` = the whole tag file, unchanged at C1 `:1-1180`
- fork @H5 `tests/hermes_cli/test_kanban_notify.py:1215-1431` = the UA-6 block
- resolved @C1 `tests/hermes_cli/test_kanban_notify.py:1183-1399` = the same 217 lines, after two blank lines

## 6. `website/docs/user-guide/configuration.md` `## Timezone` paragraph 1 (in-surface)

One paragraph keeping upstream's log-file sentence and H5's durable cron run records + the Per-profile link (ADR D1.5).
Upstream's paragraph is byte-identical at v0.22.0 and v0.22.1, so the v0.22.0 carry's merged paragraph is reused.

**upstream @tag** `website/docs/user-guide/configuration.md:2795`

> Override the server-local timezone with an IANA timezone string. Affects cron scheduling and the time injected into the system prompt. It does not change log files: every line in `~/.hermes/logs/` is stamped in the machine's local time, which is what `hermes logs --since` compares against.

**fork @H5** `website/docs/user-guide/configuration.md:2800`

> Override the server-local timezone with an IANA timezone string. It grounds cron scheduling, durable cron run records, and the bot's system-prompt clock; standard application-log timestamps stay in the host (server) zone. See [Per-profile timezone](#per-profile-timezone) below for multi-profile behavior.

**resolved @C1** `website/docs/user-guide/configuration.md:2827`

> Override the server-local timezone with an IANA timezone string. It grounds cron scheduling, durable cron run records, and the bot's system-prompt clock. It does not change log files: every line in `~/.hermes/logs/` is stamped in the machine's local time, which is what `hermes logs --since` compares against. See [Per-profile timezone](#per-profile-timezone) below for multi-profile behavior.

## 7. `plugins/nv-coworker-compose/openshell/installer.py` (#77 pick, in-surface)

Recorded with item 3 (the `-x` pick of `1fb4d74ccb`).

`git cherry-pick -x 1fb4d74ccb` onto the item-2 tip conflicts in this one file, one hunk. Both sides added a block at the same
anchor (after `_profile_dir`): H5's UPG-F67 N2 park helper (`_PROFILE_IDENTITY_MARKERS` … `_unparked_non_fleet`) and #77's
`_managed_fleet_maps`. Neither edits the other. Resolution: both, H5's first, each byte for byte, two blank lines between.
The pick commit's line multiset for the file equals the source commit's (`replay_check.py` (b)); only the hunk's position
moved. Our side of the conflict is byte-identical to the v0.22.0 carry's, so that resolution is reused unchanged.

- fork side @H5 `plugins/nv-coworker-compose/openshell/installer.py:675-707` (`a2afed6dfa`)
- fork side @#77 `plugins/nv-coworker-compose/openshell/installer.py:729-737` (`1fb4d74ccb`)
- resolved @pick `plugins/nv-coworker-compose/openshell/installer.py:746-789` (pick commit `0fe9652863`; H5's block `:746-778`,
  #77's function `:781-789`)

The other two picks are clean: #78 `b41d14df02` and #80 `35767fe243` apply with no conflict.
