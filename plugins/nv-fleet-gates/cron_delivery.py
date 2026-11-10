"""Cron delivery guard: scheduled jobs follow the same Slack posting policy as fleet_slack_post.

Off unless ``cron_delivery_guard`` is true; the allow-list is the ``slack_post`` setting
(``{<profile>: {channels, users, own_threads_only, thread_reply_only}}``).

A cron job's output is posted by the gateway to its ``deliver`` target, so ``cronjob(action='create',
deliver='slack:C...')`` would otherwise post to any channel the bot can reach, outside the profile's
``fleet_slack_post`` allow-list and without its broadcast-mention refusal. This module decides, from the
cron tool's arguments, whether a create/update/run may proceed. The ``pre_tool_call`` gate in
``__init__`` supplies the session facts (pure function here: no I/O, no core imports).

A Slack chat id may receive a job's output when it is:
  * in the profile's slack_post allow-list (channels or users), via ``allowed_targets``;
  * the conversation the job belongs to, through ``origin`` (or an omitted ``deliver``), so "remind me" jobs
    keep working: the live chat the request came from, or the stored origin of the job being updated;
  * as an explicit ``slack:<id>``: a DM conversation (D...), the requester's own user id or the home DM; a
    channel conversation (C.../G...) only as ``slack:<channel>:<its thread>``, never top-level;
  * the Slack home channel when it is a DM (``all`` and bare ``slack`` resolve there).
Every other token is refused (default-deny: the guard admits Slack deliveries only), and so is any Slack-delivered job
whose prompt holds a broadcast mention (``has_mass_mention``). ``local`` and ``bot-chat`` never reach Slack.
The fleet_slack_post thread rules hold here too: for an ``own_threads_only`` profile a ``slack:<channel>:<thread>``
on its allow-listed channel must be the job's own conversation or one of the profile's own posts there, and a
``thread_reply_only`` profile's job never reaches Slack at all (its replies go through fleet_slack_post only).

Mirrors core's resolution (cron/scheduler.py ``_resolve_single_delivery_target``,
tools/cronjob_tools.py ``_resolve_cron_context_deliver``): in a cron-run session ``origin`` (or an omitted
``deliver``) is replaced by the creating run's own target, so it is checked like an explicit target.

Other routes to a job: ``execute_code`` cannot call ``cronjob`` (it is not in core's SANDBOX_ALLOWED_TOOLS), and
``hermes cron create`` typed into ``terminal`` runs in the profile's sandbox (enforce_sandbox + ssh backend),
whose own Hermes home has no gateway, scheduler or Slack token.

Open follow-ups, outside this guard:
  * run-time output is not checked: a broadcast produced at fire time from a skill, a script or
    ``context_from`` output passes; a ``transform_llm_output`` hook that neutralises broadcast mentions in
    Slack-bound output would close it;
  * core's Slack ``format_message`` escapes ``<!here>`` before it converts Markdown links, so ``[x](!here)``
    is sent as ``<!here|x>`` (``has_mass_mention`` refuses the link form; the order bug is for upstream);
  * whether a sandbox can reach the gateway pod's ``hermes serve`` cron API is to be probed live at deploy.
"""

from __future__ import annotations

import re
from typing import Any, Optional

CRON_TOOL = "cronjob_manage"  # canonical name (legacy spelling ``cronjob``; see aliases.py)
_RUN_ACTIONS = {"run", "run_now", "trigger"}
_SLACK = "slack"
_BOT_CHAT = "bot-chat"
_REFUSED_ANYWAY = "Refused even when an allowed user asks."
# slack:<id> or slack:<id>:<thread_ts>, spaced the way core's _SLACK_THREAD_TARGET_RE accepts it.
_SLACK_TARGET = re.compile(r"\s*([^\s:]+)\s*(?::([^\s:]+)\s*)?")
_ID = re.compile(r"^[CDGU][A-Z0-9]{6,15}$")

# Broadcast mentions: the <!here> / <!here|label> control sequences and user-group pings (<!subteam^...>);
# a Markdown link whose target is one of them ([x](!here)), which Slack's Markdown converter turns into
# <!here|x>; and the typed @here/@channel/@everyone anywhere a word does not run into it (start, whitespace,
# quotes, brackets, markup), but not an address like ops@here.example.
_MASS = re.compile(
    r"<!(?:here|channel|everyone)(?:\|[^>]*)?>"
    r"|<!subteam\^"
    r"|\]\(\s*<?!(?:(?:here|channel|everyone)\b|subteam\^)"
    r"|(?<![\w@.])@(?:here|channel|everyone)\b",
    re.IGNORECASE,
)


def has_mass_mention(text: Any) -> bool:
    """True if *text* holds a broadcast mention (@here/@channel/@everyone, <!here>-style or a user group)."""
    return isinstance(text, str) and bool(_MASS.search(text))


def allowed_targets(settings: Any, profile: str) -> dict[str, set]:
    entry = (settings or {}).get(profile) if isinstance(settings, dict) else None
    if not isinstance(entry, dict):
        return {"channels": set(), "users": set()}
    return {k: {str(x) for x in (entry.get(k) or []) if isinstance(x, str) and _ID.match(x)}
            for k in ("channels", "users")}


def entry_of(settings: Any, profile: str) -> dict[str, Any]:
    entry = (settings or {}).get(profile) if isinstance(settings, dict) else None
    return entry if isinstance(entry, dict) else {}


def deliver_parts(deliver: Any) -> list[str]:
    """The tokens core delivers to, normalised exactly as core does it.

    tools/cronjob_tools.py ``_normalize_deliver_param`` joins a list/tuple with "," (``str()`` of each
    element) and the scheduler re-splits on ",", so a list element that itself holds a comma is several
    targets. Any other type is ``str()``-ed, which the default-deny token check then refuses.
    """
    if deliver is None:
        return []
    if isinstance(deliver, (list, tuple)):
        text = ",".join(str(p).strip() for p in deliver if str(p).strip())
    else:
        text = str(deliver)
    return [p.strip() for p in text.split(",") if p.strip()]


def _origin(origin: Any) -> tuple[str, str, str]:
    """``(platform, chat_id, thread_id)`` of a stored/live origin, lower-cased platform; blanks when absent."""
    if not (isinstance(origin, dict) and origin.get("platform") and origin.get("chat_id")):
        return "", "", ""
    return str(origin["platform"]).lower(), str(origin["chat_id"]), str(origin.get("thread_id") or "")


def _check(deliver: Any, *, profile: str, settings: Any, origin: Any, live_chat: str, live_thread: str,
           requester: str, home: str, own_threads: frozenset = frozenset()):
    """``(refusal or None, may_reach_slack)`` for one effective ``deliver`` value. Default-deny: a token
    that is not local / origin / all / slack / bot-chat[:<profile>] / slack:<id>[:<thread>] is refused.
    ``own_threads``: ``{"<channel>:<ts>"}`` of the profile's own posts (the ``own_post_threads`` product hook)."""
    allow = allowed_targets(settings, profile)
    entry = entry_of(settings, profile)
    if entry.get("thread_reply_only"):
        allow = {"channels": set(), "users": allow["users"]}
    own_only = bool(entry.get("own_threads_only"))
    o_platform, o_chat, o_thread = _origin(origin)
    # A bare id is trusted for the allow-list, the requester, the home DM and DM conversations. A channel
    # conversation (C.../G...) is trusted only inside its thread: a bare channel id would post top-level.
    bare = set(allow["channels"]) | set(allow["users"])
    bare |= {c for c in (requester, home, live_chat, o_chat if o_platform == _SLACK else "")
             if c and (c == requester or c.startswith("D"))}
    threads = {(c, t) for c, t in ((live_chat, live_thread), (o_chat, o_thread) if o_platform == _SLACK
                                   else ("", "")) if c and t}
    listed = sorted(allow["channels"] | allow["users"])
    allowed_note = (
        "a scheduled job may deliver only to the conversation it was created in (omit deliver, or 'origin'), "
        f"'local', the home DM, or this profile's Slack allow-list ({', '.join(listed) if listed else 'none'})"
    )

    def refuse(what: str) -> str:
        return f"cron delivery: {what} is not allowed for profile {profile!r} — {allowed_note}. {_REFUSED_ANYWAY}"

    def home_check(label: str) -> Optional[str]:
        if not home:
            return None  # nothing configured -> core delivers nothing there
        return None if home in bare and _ID.match(home) else refuse(f"{label} (resolves to {home!r})")

    slack = False
    for part in deliver_parts(deliver):
        token = part.lower()
        if token == "local" or token == _BOT_CHAT or token.startswith(_BOT_CHAT + ":"):
            continue
        if token == "origin":
            if o_platform:  # the job's own conversation (core's origin delivery)
                slack = slack or o_platform == _SLACK
                continue
            slack = slack or bool(home)  # no origin: core falls back to the home channel
            why = home_check("deliver 'origin' without an origin")
        elif token == "all":
            slack = slack or bool(home)
            why = home_check("deliver 'all'")
        elif token == _SLACK:
            if home:
                slack = True
                why = home_check("deliver 'slack'")
            else:  # core: no home -> the Slack origin conversation
                slack = slack or o_platform == _SLACK
                continue
        else:
            platform, sep, rest = part.partition(":")
            if not sep or platform.lower() != _SLACK:
                return refuse(f"deliver target {part!r} (not a delivery this fleet allows)"), slack
            slack = True
            m = _SLACK_TARGET.fullmatch(rest)
            chat, thread = (m.group(1), m.group(2) or "") if m else ("", "")
            if not (chat and _ID.match(chat)) or (thread and chat.startswith("U")):
                return refuse(f"deliver target {part!r} (use a Slack channel or user id)"), slack
            ok = chat in bare or (thread and (chat, thread) in threads)
            if ok and thread and own_only and chat in allow["channels"] and (chat, thread) not in threads \
                    and f"{chat}:{thread}" not in own_threads:
                ok = False  # own_threads_only: never a reply under someone else's message
            why = None if ok else refuse(f"deliver target {part!r}")
        if why:
            return why, slack
    return None, slack


def cron_creator_target(platform: str, chat_id: str, thread_id: str = "") -> str:
    """The concrete target core stores for ``origin`` when a cron run creates a job."""
    if not platform or not chat_id:
        return "local"
    return f"{platform}:{chat_id}:{thread_id}" if thread_id else f"{platform}:{chat_id}"


def _cron_resolved(deliver: Any, creator: str) -> list[str]:
    parts = deliver_parts(deliver) or ["origin"]
    return [creator if p.lower() == "origin" else p for p in parts]


def _is_run_with_prompt(action: str, prompt: Any) -> bool:
    return action in _RUN_ACTIONS and isinstance(prompt, str) and bool(prompt.strip())


def needs_stored_job(args: dict[str, Any]) -> bool:
    """True for the calls ``reason`` judges against the stored job: an update, or a run with extra prompt."""
    action = str(args.get("action") or "").strip().lower()
    return action == "update" or _is_run_with_prompt(action, args.get("prompt"))


def reason(args: dict[str, Any], *, profile: str, settings: Any, session: dict[str, Any],
           home: str = "", stored_job: Optional[dict[str, Any]] = None,
           own_threads: frozenset = frozenset()) -> Optional[str]:
    """Refusal message for one cron tool call, or None to let it proceed.

    ``session``: ``platform``/``chat_id``/``thread_id``/``user_id`` of the live conversation, and ``cron_creator`` (the
    creating run's concrete target) when the caller is itself a cron run. ``stored_job``: the job an
    update/run names (None when it does not exist; core then refuses the call itself).
    """
    action = str(args.get("action") or "").strip().lower()
    prompt = args.get("prompt")
    live_slack = str(session.get("platform") or "").lower() == _SLACK
    live_chat = str(session.get("chat_id") or "") if live_slack else ""
    live_thread = str(session.get("thread_id") or "") if live_slack else ""
    requester = str(session.get("user_id") or "") if live_slack else ""
    cron_creator = session.get("cron_creator")
    if cron_creator is not None:  # a cron run has no live conversation and no requester
        live_chat = live_thread = requester = ""

    if action == "create":
        if cron_creator is not None:  # core stores the run's own target for origin
            deliver: Any = _cron_resolved(args.get("deliver"), cron_creator)
            origin = None
        else:
            origin = ({"platform": session.get("platform"), "chat_id": session.get("chat_id"),
                       "thread_id": live_thread}
                      if session.get("platform") and session.get("chat_id") else None)
            # core's create_job default: an omitted deliver is the origin chat, or local without one
            deliver = args.get("deliver") if deliver_parts(args.get("deliver")) else ("origin" if origin else "local")
        content = prompt
    elif needs_stored_job(args):
        if not isinstance(stored_job, dict):
            return None  # unknown job: core refuses the call
        origin = stored_job.get("origin")
        if action == "update" and args.get("deliver") is not None:
            deliver = args.get("deliver")
            if cron_creator is not None:
                deliver = _cron_resolved(deliver, cron_creator)
        else:
            deliver = stored_job.get("deliver") or "local"
        stored_prompt = stored_job.get("prompt") if isinstance(stored_job.get("prompt"), str) else ""
        if action == "update":
            content = prompt if prompt is not None else stored_prompt
        else:
            content = f"{stored_prompt}\n{prompt}"
    else:
        return None  # list / pause / resume / remove / a plain run change neither target nor content

    refusal, may_reach_slack = _check(deliver, profile=profile, settings=settings, origin=origin,
                                      live_chat=live_chat, live_thread=live_thread, requester=requester,
                                      home=home, own_threads=frozenset(own_threads or ()))
    if refusal:
        return refusal
    if may_reach_slack and entry_of(settings, profile).get("thread_reply_only"):
        return (f"cron delivery: profile {profile!r} posts to Slack only as a reply in the CI thread its kanban task "
                f"names (fleet_slack_post); a scheduled job of it never reaches Slack. {_REFUSED_ANYWAY}")
    if may_reach_slack and has_mass_mention(content):
        return ("cron delivery: the job's prompt holds a broadcast mention (@here/@channel/@everyone or "
                f"<!here>); a job that posts to Slack never broadcasts. {_REFUSED_ANYWAY}")
    return None
