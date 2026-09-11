"""Acceptance test for RT-F02 — Router engage modes (mention | mention-sticky |
pattern | always-on) + sender_scope, rendered per profile by nv-coworker-compose.

One ``test_ac_rt_f02_<n>`` per ADR §Acceptance criteria ``pytest:`` row (AC-1..5); each
docstring's first line is ``AC-RT-F02-<n>:`` plus the criterion. AC-RT-F02-6 is a
``live:`` criterion (ADR §Scenario outlines) whose hermetic equivalent ships as the
non-criterion helper ``test_admission_path_mention_gate``. The plugin is loaded from
an isolated HERMES_HOME with an EMPTY bundled dir through the real
``PluginManager().discover_and_load()`` path (shape: tests/hermes_cli/
test_plugin_api_compat.py:14-52); ``compose()`` is driven and the render asserted through
the real core parser ``gateway.config.PlatformConfig.from_dict`` (the golden value lands
in the exact ``.extra`` dict the resolver reads) plus the real platform resolvers when
the platform extra is installed.

Run with the Slack platform extra so the adapter imports resolve:
``uv sync --locked --python 3.11 --extra slack --extra dev`` (CI ``--extra all``). The
platform adapters ``import aiohttp`` at module top; the resolver / admission-path drives
are gated on ``importlib.util.find_spec("aiohttp")`` and AC-6 uses ``importorskip``.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
DOC_PAGE = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-engage-modes.md"

_PAT = r"\bhermes\b"
_CHAN = "C-on"
_SLACK_ENV = (
    "SLACK_REQUIRE_MENTION", "SLACK_STRICT_MENTION", "SLACK_THREAD_REQUIRE_MENTION",
    "SLACK_FREE_RESPONSE_CHANNELS", "SLACK_ALLOWED_CHANNELS",
    "SLACK_REQUIRE_MENTION_CHANNELS", "SLACK_MENTION_PATTERNS",
)
_DISCORD_ENV = ("DISCORD_REQUIRE_MENTION", "DISCORD_ALLOWED_CHANNELS", "DISCORD_FREE_RESPONSE_CHANNELS")
_TELEGRAM_ENV = ("TELEGRAM_REQUIRE_MENTION", "TELEGRAM_ALLOWED_CHATS", "TELEGRAM_FREE_RESPONSE_CHATS")

# The full capability table's expected PlatformConfig.extra per (platform, mode). The
# scope key (allowed_channels/allowed_chats) is added separately only when sender_scope
# is declared, so it is NOT part of these mode-only golden dicts.
_CAPS_EXPECTED = {
    ("slack", "mention"): {"require_mention": True, "strict_mention": True,
                           "thread_require_mention": True, "mention_patterns": [],
                           "free_response_channels": [], "require_mention_channels": []},
    ("slack", "mention-sticky"): {"require_mention": True, "strict_mention": False,
                                  "thread_require_mention": False, "mention_patterns": [],
                                  "free_response_channels": [], "require_mention_channels": []},
    ("slack", "pattern"): {"require_mention": True, "strict_mention": True,
                           "thread_require_mention": True, "mention_patterns": [_PAT],
                           "free_response_channels": [], "require_mention_channels": []},
    ("slack", "always-on"): {"require_mention": True, "strict_mention": False,
                             "thread_require_mention": False, "mention_patterns": [],
                             "free_response_channels": [_CHAN], "require_mention_channels": []},
    ("discord", "mention"): {"require_mention": True, "thread_require_mention": True,
                             "free_response_channels": []},
    ("discord", "mention-sticky"): {"require_mention": True, "thread_require_mention": False,
                                    "free_response_channels": []},
    ("discord", "always-on"): {"require_mention": True, "thread_require_mention": False,
                               "free_response_channels": [_CHAN]},
    ("telegram", "mention"): {"require_mention": True, "mention_patterns": [],
                              "free_response_chats": [], "observe_unmentioned_group_messages": False},
    ("telegram", "pattern"): {"require_mention": True, "mention_patterns": [_PAT],
                              "free_response_chats": [], "observe_unmentioned_group_messages": False},
    ("telegram", "always-on"): {"require_mention": True, "mention_patterns": [],
                                "free_response_chats": [_CHAN], "observe_unmentioned_group_messages": False},
}
_SCOPE_KEY = {"slack": "allowed_channels", "discord": "allowed_channels", "telegram": "allowed_chats"}


def _engage_for(platform: str, mode: str, sender_scope=None) -> dict:
    block: dict = {"mode": mode}
    if mode == "pattern":
        block["patterns"] = [_PAT]
    if mode == "always-on":
        block["channels"] = [_CHAN]
    if sender_scope is not None:
        block["sender_scope"] = sender_scope
    return {platform: block}


# ── fixture spec + loader ────────────────────────────────────────────────────────
def _write_fixture_spec(spec_dir: Path, spec: dict) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spines").mkdir(exist_ok=True)
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "Base.", "invariants": ["Be helpful."],
                        "workflows": ["wf"], "config": {}}, sort_keys=False),
        encoding="utf-8")
    wf = spec_dir / "workflows" / "wf"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "SKILL.md").write_text("# wf\n\nDo the work.\n", encoding="utf-8")
    (spec_dir / "skills").mkdir(exist_ok=True)
    p = spec_dir / "coworker-types.yaml"
    p.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return p


def _base_spec() -> dict:
    return {
        "orchestrator_profile": "orchestrator",
        "default_profile": "default",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": {"orchestrator": {"extends": ["base"], "config": {}},
                  "worker": {"extends": ["base"], "config": {}}},
        "default_config": {},
    }


def _spec_with_engage(engage: dict, extra_worker_config: dict | None = None) -> dict:
    """Place the same engage block on both coworker types AND the default profile so the
    engage-pseudo-key-removal assertion (AC-1) is exercised on every rendered profile."""
    spec = _base_spec()
    for t in spec["types"].values():
        t["config"]["engage"] = copy.deepcopy(engage)
    spec["default_config"]["engage"] = copy.deepcopy(engage)
    if extra_worker_config:
        spec["types"]["worker"]["config"].update(copy.deepcopy(extra_worker_config))
    return spec


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8")
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    for var in (*_SLACK_ENV, *_DISCORD_ENV, *_TELEGRAM_ENV):
        monkeypatch.delenv(var, raising=False)

    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True
    assert entry.error is None
    assert entry.module is not None
    return entry.module, entry


def _render(module, tmp_path, spec: dict, name: str) -> Path:
    out = tmp_path / f"out_{name}"
    module.compose(str(_write_fixture_spec(tmp_path / f"spec_{name}", spec)), str(out))
    return out


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _worker_config(out: Path) -> dict:
    return _read_yaml(out / "worker" / "config.yaml")


def _extra(cfg: dict, platform: str) -> dict:
    # the rendered PlatformConfig.extra via the REAL core parser (gateway/config.py:724)
    from gateway.config import PlatformConfig
    return dict(PlatformConfig.from_dict(cfg.get("platforms", {}).get(platform, {})).extra)


def _has_aiohttp() -> bool:
    return importlib.util.find_spec("aiohttp") is not None


# ── AC-RT-F02-1 ──────────────────────────────────────────────────────────────────
def test_ac_rt_f02_1(loaded, tmp_path):
    """AC-RT-F02-1: For a Slack coworker, engage mode `mention` renders require_mention/
    strict_mention/thread_require_mention true with empty patterns/free-response/
    require_mention_channels, and `pattern` renders the same with a non-empty
    mention_patterns; the inert config.engage pseudo-key is removed from every rendered
    profile (worker, orchestrator, default)."""
    module, _entry = loaded

    out = _render(module, tmp_path, _spec_with_engage(_engage_for("slack", "mention")), "s_m")
    for prof in ("worker", "orchestrator", "default"):
        assert "engage" not in _read_yaml(out / prof / "config.yaml"), prof
    assert _extra(_worker_config(out), "slack") == _CAPS_EXPECTED[("slack", "mention")]

    outp = _render(module, tmp_path, _spec_with_engage(_engage_for("slack", "pattern")), "s_p")
    assert _extra(_worker_config(outp), "slack") == _CAPS_EXPECTED[("slack", "pattern")]

    if _has_aiohttp():
        from gateway.config import PlatformConfig
        from plugins.platforms.slack.adapter import SlackAdapter
        a = SlackAdapter(PlatformConfig(extra=_extra(_worker_config(out), "slack")))
        assert a._slack_require_mention() is True
        assert a._slack_strict_mention() is True
        assert a._slack_thread_require_mention() is True
        assert a._slack_mention_patterns() == []
        ap = SlackAdapter(PlatformConfig(extra=_extra(_worker_config(outp), "slack")))
        assert len(ap._slack_mention_patterns()) == 1  # one compiled wake-word pattern


# ── AC-RT-F02-2 ──────────────────────────────────────────────────────────────────
def test_ac_rt_f02_2(loaded, tmp_path):
    """AC-RT-F02-2: Engage mode `mention-sticky` renders require_mention true with
    strict_mention and thread_require_mention false (sticky on) and wins deterministically
    over a contradicting inherited value planted at the root slack block, platforms.slack,
    gateway.platforms.slack and gateway.slack (and their extra) — the controlled keys
    survive only at platforms.slack.extra."""
    module, _entry = loaded

    contradiction = {
        "slack": {"require_mention": False, "strict_mention": True,
                  "extra": {"thread_require_mention": True}},
        "platforms": {"slack": {"require_mention": False,
                                "extra": {"strict_mention": True, "free_response_channels": ["Cx"]}}},
        "gateway": {"slack": {"strict_mention": True, "extra": {"require_mention": False}},
                    "platforms": {"slack": {"require_mention": False,
                                            "extra": {"thread_require_mention": True}}}},
    }
    out = _render(module, tmp_path,
                  _spec_with_engage(_engage_for("slack", "mention-sticky"), contradiction), "sticky")
    cfg = _worker_config(out)
    controlled = set(_CAPS_EXPECTED[("slack", "mention-sticky")])

    def _plain(node):
        return set(node or {}) & controlled

    def _in_extra(node):
        return set((node or {}).get("extra", {})) & controlled

    gw = cfg.get("gateway") or {}
    assert _plain(cfg.get("slack")) == set() and _in_extra(cfg.get("slack")) == set()
    assert _plain(cfg.get("platforms", {}).get("slack")) == set()
    assert _plain(gw.get("slack")) == set() and _in_extra(gw.get("slack")) == set()
    gwp = gw.get("platforms", {}).get("slack")
    assert _plain(gwp) == set() and _in_extra(gwp) == set()

    # platforms.slack.extra holds EXACTLY the canonical set (the contradicting
    # free_response_channels:["Cx"] is overwritten to [])
    assert _extra(cfg, "slack") == _CAPS_EXPECTED[("slack", "mention-sticky")]

    if _has_aiohttp():
        from gateway.config import PlatformConfig
        from plugins.platforms.slack.adapter import SlackAdapter
        a = SlackAdapter(PlatformConfig(extra=_extra(cfg, "slack")))
        assert a._slack_strict_mention() is False  # sticky guards both false ->
        assert a._slack_thread_require_mention() is False  # _register_mentioned_thread enabled


# ── AC-RT-F02-3 ──────────────────────────────────────────────────────────────────
def test_ac_rt_f02_3(loaded, tmp_path):
    """AC-RT-F02-3: Engage mode `always-on` renders a bounded free_response_channels
    (Slack/Discord) / free_response_chats (Telegram) and declared sender_scope renders
    allowed_channels (Slack/Discord) / allowed_chats (Telegram); Telegram uses its
    chat-scoped key names while Discord's spellings match Slack; omitting sender_scope
    preserves an inherited channel restriction while declaring it replaces it."""
    module, _entry = loaded

    for (platform, mode), expected in _CAPS_EXPECTED.items():
        out = _render(module, tmp_path, _spec_with_engage(_engage_for(platform, mode)),
                      f"{platform}_{mode}")
        got = _extra(_worker_config(out), platform)
        assert got == expected, (platform, mode, got)
        if platform == "telegram":  # Telegram is chat-scoped, not the Slack channel keys
            assert "free_response_channels" not in got and "allowed_channels" not in got

    for platform, scope_key in _SCOPE_KEY.items():
        out = _render(module, tmp_path,
                      _spec_with_engage(_engage_for(platform, "mention", sender_scope=["C9"])),
                      f"{platform}_scope")
        assert _extra(_worker_config(out), platform)[scope_key] == ["C9"]

    inherited = {"platforms": {"slack": {"extra": {"allowed_channels": ["C-parent"]}}}}
    out = _render(module, tmp_path,
                  _spec_with_engage(_engage_for("slack", "mention-sticky"), inherited), "keepscope")
    assert _extra(_worker_config(out), "slack")["allowed_channels"] == ["C-parent"]
    out2 = _render(module, tmp_path,
                   _spec_with_engage(_engage_for("slack", "mention-sticky", sender_scope=["C-new"]),
                                     inherited), "replacescope")
    assert _extra(_worker_config(out2), "slack")["allowed_channels"] == ["C-new"]

    if _has_aiohttp():
        from gateway.config import PlatformConfig
        from plugins.platforms.slack.adapter import SlackAdapter
        from plugins.platforms.discord.adapter import DiscordAdapter
        from plugins.platforms.telegram.adapter import TelegramAdapter
        s = _render(module, tmp_path, _spec_with_engage(_engage_for("slack", "always-on")), "s_on")
        sa = SlackAdapter(PlatformConfig(extra=_extra(_worker_config(s), "slack")))
        assert sa._slack_free_response_channels() == {_CHAN}
        # Discord/Telegram flag resolvers read only self.config.extra -> unbound on a stub
        d = _render(module, tmp_path, _spec_with_engage(_engage_for("discord", "mention")), "d_m")
        dstub = SimpleNamespace(config=PlatformConfig(extra=_extra(_worker_config(d), "discord")))
        assert DiscordAdapter._discord_require_mention(dstub) is True
        assert DiscordAdapter._discord_thread_require_mention(dstub) is True
        t = _render(module, tmp_path, _spec_with_engage(_engage_for("telegram", "mention")), "t_m")
        tstub = SimpleNamespace(config=PlatformConfig(extra=_extra(_worker_config(t), "telegram")))
        assert TelegramAdapter._telegram_require_mention(tstub) is True


# ── AC-RT-F02-4 ──────────────────────────────────────────────────────────────────
def test_ac_rt_f02_4(loaded, tmp_path):
    """AC-RT-F02-4: The plugin registers no pre_gateway_dispatch (routing) hook, and the
    render fails closed with CompositionError on a blanket-forward always-on (no bounded
    channels, empty, or a wildcard including a ["*"] list element), pattern on Discord,
    mention-sticky on Telegram, pattern without patterns, an unknown mode, an empty/wildcard
    sender_scope, and any unsupported platform."""
    module, entry = loaded

    assert "pre_gateway_dispatch" not in entry.hooks_registered

    def _reject(engage: dict, tag: str):
        with pytest.raises(module.CompositionError):
            _render(module, tmp_path, _spec_with_engage(engage), tag)

    _reject({"slack": {"mode": "always-on"}}, "on_no_channels")
    _reject({"slack": {"mode": "always-on", "channels": []}}, "on_empty")
    _reject({"slack": {"mode": "always-on", "channels": "*"}}, "on_wildcard_scalar")
    _reject({"discord": {"mode": "always-on", "channels": ["*"]}}, "on_wildcard_list")
    _reject({"discord": {"mode": "pattern", "patterns": ["x"]}}, "disc_pattern")
    _reject({"telegram": {"mode": "mention-sticky"}}, "tg_sticky")
    _reject({"slack": {"mode": "pattern"}}, "pattern_no_patterns")
    _reject({"slack": {"mode": "banter"}}, "unknown_mode")
    _reject({"irc": {"mode": "mention"}}, "unsupported_platform")
    # an empty/wildcard sender_scope silently widens access (empty allowlist == no
    # restriction), so it is rejected rather than rendered
    _reject({"slack": {"mode": "mention", "sender_scope": []}}, "scope_empty")
    _reject({"slack": {"mode": "mention", "sender_scope": "*"}}, "scope_wildcard_scalar")
    _reject({"slack": {"mode": "mention", "sender_scope": ["*"]}}, "scope_wildcard")
    _reject({"slack": {"mode": "mention", "sender_scope": ["C9", "*"]}}, "scope_wildcard_member")


# ── AC-RT-F02-5 ──────────────────────────────────────────────────────────────────
def test_ac_rt_f02_5(loaded):
    """AC-RT-F02-5: The doc page maps every supported (platform, engage mode) to its
    exact Hermes key set and lists the unsupported combinations, and records that
    sender_scope is channel/chat scope (per-user admission is RT-F01/RT-F05), room
    deliberation is A2A-F18, the no-router/never-blanket-forward invariant, the Discord
    env-precedence caveat, and that Slack mention-sticky is the native equivalent
    (broader than NanoClaw), not exact parity."""
    assert DOC_PAGE.is_file(), f"missing doc page {DOC_PAGE}"
    text = DOC_PAGE.read_text(encoding="utf-8")
    import re as _re

    matched_rows = 0
    rows: dict[tuple[str, str], set] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if len(cells) >= 3 and (cells[0], cells[1]) in _CAPS_EXPECTED:
            matched_rows += 1
            raw_keys = [part.strip().strip("`") for part in cells[2].split(",") if part.strip()]
            assert all(_re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in raw_keys), cells[2]
            rows[(cells[0], cells[1])] = set(raw_keys)
    assert matched_rows == len(_CAPS_EXPECTED), f"expected {len(_CAPS_EXPECTED)} table rows, matched {matched_rows}"
    for key, expected in _CAPS_EXPECTED.items():
        assert key in rows, f"doc table missing row for {key}"
        assert rows[key] == set(expected), (key, rows[key], set(expected))

    low = text.lower()

    def _line_has(*needles):
        return any(all(n in ln.lower() for n in needles) for ln in text.splitlines())
    unsupported = ("unsupported", "not supported", "compositionerror")
    assert any(_line_has("discord", "pattern", m) for m in unsupported), "discord/pattern unsupported not documented"
    assert any(_line_has("telegram", "mention-sticky", m) for m in unsupported), "telegram/mention-sticky unsupported not documented"

    assert "A2A-F18" in text, "room deliberation ownership (A2A-F18) not documented"
    assert "blanket-forward" in low, "never-blanket-forward invariant not documented"
    assert "sender_scope" in text and ("allow_from" in text or "RT-F01" in text or "RT-F05" in text)
    assert "pre_gateway_dispatch" in text or "no router" in low or "no hook" in low
    assert "only `allowed_channels` is env-first" in text
    assert "DISCORD_ALLOWED_CHANNELS" in text
    assert "read `.extra` first" in text
    assert "parity" in low or "broader" in low


# ── additional hermetic coverage for AC-RT-F02-6 (live) ──────────────────────────
# AC-RT-F02-6 is a live: criterion (ADR §Scenario outlines). This is the hermetic
# admission-path proof of the same behaviour; it is deliberately NOT named test_ac_rt_f02_*
# so an aiohttp skip on a minimal venv never fails an AC criterion.
def test_admission_path_mention_gate(loaded, tmp_path):
    """Hermetic coverage for AC-RT-F02-6: a mention-mode Slack render gates messages through
    the real admission path — un-mentioned group message -> no turn, mentioned -> one turn,
    thread follow-up without re-mention -> no further turn (strict disables sticky)."""
    pytest.importorskip("aiohttp")  # non-criterion coverage; runs only with the slack extra
    module, _entry = loaded
    from gateway.config import PlatformConfig
    from plugins.platforms.slack.adapter import SlackAdapter

    out = _render(module, tmp_path, _spec_with_engage(_engage_for("slack", "mention")), "admit")
    adapter = SlackAdapter(PlatformConfig(extra=_extra(_worker_config(out), "slack")))
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids["T1"] = "UBOT"
    adapter._has_active_session_for_thread = lambda **_: False

    async def _empty(**_):
        return ""

    async def _name(*_a, **_k):
        return "User"

    adapter._fetch_thread_context = _empty
    adapter._fetch_thread_parent_text = _empty
    adapter._resolve_user_name = _name

    handled: list = []

    async def _capture(event):
        handled.append(event)

    adapter.handle_message = _capture

    def _event(text, ts, thread_ts=None):
        e = {"type": "message", "channel": "C123", "channel_type": "channel",
             "team": "T1", "user": "U123", "text": text, "ts": ts}
        if thread_ts is not None:
            e["thread_ts"] = thread_ts
        return e

    run = asyncio.run
    run(adapter._handle_slack_message(_event("vpn is down", "100.0")))
    assert handled == [], "un-mentioned group message must not engage"
    run(adapter._handle_slack_message(_event("<@UBOT> help", "101.0")))
    assert len(handled) == 1, "mentioned message must engage"
    run(adapter._handle_slack_message(_event("<@UBOT> start", "102.0", thread_ts="102.0")))
    assert len(handled) == 2
    # strict_mention disables the mentioned-thread sticky wake (this is the discriminator)
    run(adapter._handle_slack_message(_event("follow up", "103.0", thread_ts="102.0")))
    assert len(handled) == 2, "strict mention must not sticky-wake an un-mentioned follow-up"
