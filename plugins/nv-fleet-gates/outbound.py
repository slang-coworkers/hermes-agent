"""Outbound write policy: no card, no write; and every forge write the critique gate covers.

Off unless ``chain.outbound_policy`` is true. Work happens on chain cards. Every OUTBOUND write a coworker makes
(worker roles and the orchestrator alike) needs a kanban card this execution owns (``__init__._owned_card``): the
dispatcher-owned ``HERMES_KANBAN_TASK`` (``agent.delegation_context.owned_kanban_task()``) with its run id and claim
lock, and on the board that card assigned to this profile, ``running``, on that run and claim (the claim's expiry is
not checked: the dispatcher extends a live worker's expired claim, so expiry says nothing about this run). The
``pre_tool_call`` gate in ``__init__`` gathers those facts; this module classifies a call and decides.

Outbound writes (``classify``), read from the commands the text runs in EXECUTABLE position (commands.py: an
argument such as ``--body`` or ``-m``, and a heredoc fed to anything but a shell or Python, is data):
  git-push   ``git push`` / ``git send-pack`` (``git -C dir push``, ``/usr/lib/git-core/git-push``, ``bash -c``,
             ``$(...)``, a Python ``["git", "push", ...]`` list or ``subprocess``/``os.system`` string)
  gitlab     a write by a forge CLI named in the ``forge_tools`` setting, parsed with that CLI's OWN argparse schema
             when the ``forge_cli_parser`` product hook supplies it (its noun, verb, ``--project`` and the iid
             argparse binds, never a nearby integer; no option abbreviations); any verb it does not know to be a
             read; an invocation its parser refuses (fail closed); the client imported in Python (opaque). The CLI
             is found by name, through ``python3 -m <module>`` or a path to its script, package or ``__main__.py``,
             through a variable, alias or shell function set earlier in the same call (``G="<cli> --project p";
             $G issue comment ...``), and a command word left unresolved (``$G ...``) counts as an opaque forge
             write when the text mentions the CLI (and as a push when it mentions ``git push``). Without a parser
             every invocation that is not a plain read is a write to an unknown target
  slack      ``fleet_slack_post``; ``send_message`` with action send; a raw Slack write (a Web API write method, an
             incoming webhook, a Slack SDK write call) or ``hermes send``
  kanban     ``kanban_complete``, ``kanban_request_review``, ``kanban_request_changes``
  script     a program the gate cannot read: ``bash|sh|... <file>``, ``python3 <file>``, ``source``/``.``, or a
             path run directly (``./push.sh``). It may push or post, so a card-less role may not run one; with a
             card it passes (and restales the critique). ``python3 -c``, ``bash -c`` and heredocs are read instead.
Reads, local sandbox edits and the other kanban tools stay open.

Who may write (``decide``):
  * nobody marks a merge request ready (``mr update --ready``): a coworker's MR stays a draft for every role, card
    or not;
  * a session that owns its running card: yes (then the other gates: critique, plan), except that the
    orchestrator never pushes, never opens or edits a merge request, and never makes an opaque forge write (the
    imported client, an invocation the parser refuses, a noun it does not know);
  * the orchestrator without a card, routing only: a reply in ITS OWN originating conversation (a
    ``fleet_slack_post`` / ``send_message`` to the Slack conversation or thread the session is in, or that a cron
    run delivers to; a ``<cli> --project <p> <issue|mr> comment <iid>`` on the issue or MR whose authenticated
    webhook delivery started the session, as conversation_origin.py recorded it: the iid AND the ``--project`` the
    parser binds must both match), and a TOP-LEVEL ``fleet_slack_post`` (no ``thread_ts``) to a channel on its own
    ``slack_post`` allow-list (a chain status post), from any session;
  * everyone else: refused, "work happens on chain cards: ask the orchestrator to open one".
A hosted group room (session title ``Group: <room_id>``) never holds a card and gets no own-conversation reply,
and ``kanban_create`` from a room is refused unless the profile is in ``chain.card_creator_profiles``.

Critique (``critique_egress``): every forge write needs a fresh critique, as ``gh pr create|review|comment`` does,
with ONE exception, ``mr explain`` (its flow is explain -> critique -> complete, so the terminator's own critique
covers the note). That includes the orchestrator's reply in its own conversation, label and state changes, and
verbs this module does not know. A ``git push`` needs one only when ``critique_push_remotes`` (regexes over
``/<project path>``) is set: then a push to a matching remote, or to one that cannot be read from the command (a
named remote such as ``origin``: fail closed), does. The text of such a write must come from a file
(``--body-file`` / ``--description-file``, never ``-``): an inline ``--body`` or ``--description`` was never in
front of the critique (a title stays an argument). Anything that may change the tree after the critique restales
it, whether it succeeded or not: a file write, ``git cherry-pick|merge|pull|rebase|am|revert|...``,
``make|cmake|ninja``, any shell or Python program (predicates.restales_critique; these stay out of the plan gate).
The critique is bound by ORDER, not by content.

Limits: it reads text and cannot follow what the text computes (a command assembled in steps). And the card is
checked when the tool call starts: a card completed, cancelled or reassigned while a long command runs is not seen
(a pre-tool gate cannot close that window).
"""

from __future__ import annotations

import argparse
import functools
import logging
import re
from typing import Any, Callable, NamedTuple, Optional

from . import commands, product_hooks

logger = logging.getLogger(__name__)

NO_CARD = "work happens on chain cards: ask the orchestrator to open one"
_HEAD = "outbound write policy (nv-fleet-gates): "

_TEXT_TOOLS = frozenset({"terminal", "execute_code", "process_manage"})
KANBAN_TERMINATORS = frozenset({"kanban_complete", "kanban_request_review", "kanban_request_changes"})
SLACK_TOOLS = frozenset({"fleet_slack_post", "send_message"})
KANBAN_CREATE = "kanban_create"

# Forge CLI (noun, verb) pairs that only read; any other pair is a write (fail closed). Includes the read verbs
# the open chain branches add (mr discussions, pipeline test-report, mr note-check).
_FORGE_READS = frozenset({
    ("issue", "get"), ("issue", "list"), ("issue", "notes"),
    ("mr", "get"), ("mr", "list"), ("mr", "notes"), ("mr", "diffs"), ("mr", "pipelines"),
    ("mr", "discussions"), ("mr", "note-check"),
    ("pipeline", "list"), ("pipeline", "jobs"), ("pipeline", "test-report"), ("job", "log"), ("job", "artifacts"),
    ("group", "members"), ("whoami", ""),
})
# The one GitLab write that needs no critique of its own (explain -> critique -> complete).
_NO_CRITIQUE = frozenset({("mr", "explain")})
# Replies the orchestrator may make in its own GitLab conversation (the note the webhook route asks for:
# `<issue|mr> comment <iid>`, or a reply in a thread of that MR), and the conversation_origin kind of each noun.
_REPLY_VERBS = frozenset({("issue", "comment"), ("mr", "comment"), ("mr", "reply")})
_ITEM_KIND = {"issue": "issue", "mr": "merge_request"}
_PUSH_VALUE_OPTS = frozenset({"--repo", "--receive-pack", "--exec", "-o", "--push-option"})
_PUSH_MENTION_RE = re.compile(r"(?<![\w.-])git(?![\w.-])[^\n]*?(?<![\w.:/-])(?:push|send-pack)(?![\w.:/-])|git-push\b")
_UNRESOLVED_RE = re.compile(r"[\"']?\$")
# critique_push_remotes, compiled; empty: a push needs no critique.
_PUSH_REMOTES: tuple = ()


def configure(critique_push_remotes=()) -> None:
    """Set the remotes a push needs a fresh critique for (nv-fleet-gates' ``critique_push_remotes`` setting)."""
    global _PUSH_REMOTES
    compiled = []
    for pattern in critique_push_remotes or ():
        try:
            compiled.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error:
            # fail closed: a pattern that does not compile matches every remote
            logger.warning("nv-fleet-gates: critique_push_remotes entry does not compile; every push needs a critique")
            compiled.append(re.compile(""))
    _PUSH_REMOTES = tuple(compiled)


@functools.lru_cache(maxsize=8)
def _forge_res(tools: frozenset, modules: frozenset) -> tuple:
    """(mention, import) patterns for the configured forge CLIs; ``None`` each when none is named."""
    if not tools:
        return None, None
    mention = "|".join("[-_]".join(map(re.escape, re.split(r"[-_]", t))) for t in sorted(tools))
    names = "|".join(map(re.escape, sorted(modules)))
    return (re.compile(mention),
            re.compile(r"(?m)(?:^|[;\"'])\s*(?:import\s+(?:" + names + r")\b|from\s+(?:" + names
                       + r")(?:\.\w+)*\s+import\b)"))


class Write(NamedTuple):
    """One outbound write a call makes."""

    kind: str           # git-push | gitlab | slack | kanban
    noun: str = ""      # gitlab: issue / mr / ... ("client", "?" when opaque); slack + kanban: the tool
    verb: str = ""      # gitlab: the verb
    project: str = ""   # gitlab: the --project value; git-push: the remote's project path ("" = a named remote)
    iid: Optional[int] = None
    target: str = ""    # slack: the target; git-push: the remote as written
    thread: str = ""    # slack: thread_ts
    inline: bool = False  # gitlab: text given inline (--body / --description / a "-" file), not from a file
    ready: bool = False   # gitlab: mr update --ready (never: a bot's MR stays a draft)

    def label(self) -> str:
        """For the audit line and the refusal: what was written, never the text."""
        if self.kind == "gitlab":
            return f"gitlab:{self.noun} {self.verb}".strip()
        if self.kind in ("slack", "kanban", "script"):
            return f"{self.kind}:{self.noun}"
        return self.kind


# --------------------------------------------------------------------------- classification
def classify(canon: str, args: Any, *, home: Optional[str] = None) -> list[Write]:
    """Every outbound write the call makes (empty: none). May raise; the gate fails closed on it.

    ``home`` is the resolved HERMES_HOME whose ``forge_cli_parser`` registration reads a forge CLI call."""
    args = args if isinstance(args, dict) else {}
    if canon in KANBAN_TERMINATORS:
        return [Write("kanban", canon)]
    if canon == "fleet_slack_post":
        return [Write("slack", canon, target=commands._str(args.get("target")).strip(),
                      thread=commands._str(args.get("thread_ts")).strip())]
    if canon == "send_message":
        if commands._str(args.get("action") or "send").strip().lower() != "send":
            return []
        return [Write("slack", canon, target=commands._str(args.get("target")).strip())]
    if canon not in _TEXT_TOOLS:
        return []
    out: list[Write] = []
    for text, mode, _scope in commands._texts(canon, args):
        out.extend(_text_writes(text, mode, home))
    seen: dict = {}
    for w in out:  # one entry per distinct write, in order
        seen.setdefault(w, w)
    return list(seen.values())


def _text_writes(text: str, mode: str, home: Optional[str] = None) -> list[Write]:
    if not text.strip():
        return []
    out: list[Write] = []
    mention_re, import_re = _forge_res(commands.FORGE_TOOLS, commands._FORGE_MODULES)
    mentions_forge = bool(mention_re and mention_re.search(text))
    for argv in commands.runs(text, mode):
        if argv and _UNRESOLVED_RE.match(argv[0]):  # a command word this call cannot resolve ($G, ${G})
            if mentions_forge:
                out.append(Write("gitlab", "?", "?"))
            if _PUSH_MENTION_RE.search(text):
                out.append(Write("git-push"))
            continue
        if commands.script_run(argv):  # a script the gate cannot read: it may push or post
            out.append(Write("script", commands.basename(argv[0])))
            continue
        call = commands.git_call(argv)
        if call and call[0] in ("push", "send-pack"):
            remote = _push_remote(call[1])
            out.append(Write("git-push", project=_remote_project(remote), target=remote))
        forge = commands.forge_args(argv)
        if forge is not None:
            w = forge_op(forge, home=home)
            if w is not None:
                out.append(w)
        if commands.hermes_send(argv):
            out.append(Write("slack", "hermes-send"))
    if commands._SLACK_WRITE_RE.search(text):
        out.append(Write("slack", "raw"))
    if import_re and import_re.search(text):
        out.append(Write("gitlab", "client", "python"))  # the client's write methods, called from Python: opaque
    return out


def _push_remote(args: list[str]) -> str:
    skip = False
    for t in args:
        if skip:
            skip = False
        elif t in _PUSH_VALUE_OPTS:
            skip = True
        elif t == "--":
            continue
        elif not t.startswith("-"):
            return t
    return ""


def _remote_project(remote: str) -> str:
    """The project path of a remote written as a URL (``group/project``), else "" (a named remote)."""
    if "://" in remote:
        rest = remote.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    elif re.match(r"[\w.-]+@[\w.-]+:", remote):
        path = remote.split(":", 1)[1]
    else:
        return ""
    return path.split("?", 1)[0].strip("/").removesuffix(".git")


# --------------------------------------------------------------------------- the forge CLI's own parser
class _ParseExit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


_warned_no_parser = False


def forge_parser(home: Optional[str] = None) -> Optional[argparse.ArgumentParser]:
    """The forge CLI's own ``argparse`` parser from the ``forge_cli_parser`` product hook of ``home``, made silent:
    it never prints and never exits, so a refusal or ``--help`` is an exception the classifier reads. None when no
    plugin supplies one. Asked on every call, so a registration made or replaced later is seen."""
    if home is None:
        return None
    parser = product_hooks.call("forge_cli_parser", home=home, default=None)
    if parser is not None:
        _silence(parser)
    return parser


def _silence(parser: argparse.ArgumentParser) -> None:
    def _exit(status=0, message=None):
        raise _ParseExit(int(status or 0))

    def _error(message):
        raise _ParseExit(2)

    parser.exit, parser.error = _exit, _error
    parser.print_help = parser.print_usage = lambda *a, **k: None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in set(action.choices.values()):
                _silence(sub)


def forge_op(args: list[str], home: Optional[str] = None) -> Optional[Write]:
    """The write one forge CLI invocation makes (None for a read), from argparse's own reading of ``args``."""
    global _warned_no_parser
    parser = forge_parser(home)
    if parser is None:
        if not _warned_no_parser:
            _warned_no_parser = True
            logger.warning("nv-fleet-gates outbound policy: no forge CLI parser registered; every forge "
                           "invocation that is not a plain read counts as a write to an unknown target")
        return _forge_op_without_parser(args)
    try:
        ns, _unknown = parser.parse_known_args(list(args))  # an option this parser lacks (a newer CLI) is ignored
    except _ParseExit as exc:
        if exc.code == 0:
            return None  # --help: prints usage, sends nothing
        if _forge_op_without_parser(args) is None:
            # A read verb its parser refuses (e.g. `mr note-check` without its required --kind): the CLI exits
            # on the same parse error, so nothing is sent. Every other refusal stays an unknown write.
            return None
        return Write("gitlab", "?", "?")  # its parser refuses it (an unknown noun or verb, a missing argument)
    noun, verb = str(getattr(ns, "noun", "") or ""), str(getattr(ns, "verb", "") or "")
    if (noun, verb) in _FORGE_READS:
        return None
    if (noun, verb) == ("mr", "explain") and (getattr(ns, "quiz_positions", False) or getattr(ns, "dry_run", False)):
        return None  # the quiz-position helper and the dry run print only
    iid = getattr(ns, "iid", None)
    project = getattr(ns, "project", None)
    inline = any(getattr(ns, name, None) is not None for name in ("body", "description")) or any(
        getattr(ns, name, None) == "-" for name in ("body_file", "description_file", "explanation_file"))
    return Write("gitlab", noun, verb, project=project if isinstance(project, str) else "",
                 iid=iid if isinstance(iid, int) and not isinstance(iid, bool) else None,
                 inline=inline, ready=(noun, verb) == ("mr", "update") and getattr(ns, "draft", None) is False)


def _forge_op_without_parser(args: list[str]) -> Optional[Write]:
    words, i = [], 0
    while i < len(args):
        if args[i] == "--project":
            i += 2
            continue
        if args[i] in ("-h", "--help"):
            return None
        if not args[i].startswith("-"):
            words.append(args[i])
        i += 1
    noun, verb = (words + ["", ""])[:2]
    if noun == "whoami":
        verb = ""
    if (noun, verb) in _FORGE_READS:
        return None
    return Write("gitlab", noun or "?", verb or "?")  # no target: never an own-conversation reply


# --------------------------------------------------------------------------- decision
class Facts(NamedTuple):
    """What the gate knows about the calling execution (gathered in ``__init__``)."""

    profile: str
    orchestrator: bool
    card: str           # the owned, running card assigned to this profile; "" when there is none
    room: bool          # a hosted group room (or a session that cannot be shown not to be one)
    own: Callable[[Write], bool]   # the write is a reply in this session's own originating conversation


def decide(writes: list[Write], facts: Facts) -> Optional[tuple[Write, str, str]]:
    """``(write, rule, refusal)`` for the first write the policy refuses, else None."""
    for w in writes:
        if w.kind == "gitlab" and w.ready:
            return w, "never-ready", _refusal(
                w, "a coworker's merge request stays a draft; a human marks it ready (say so and ask a reviewer)")
        if facts.orchestrator:
            rule = _orchestrator_never(w)
            if rule:
                return w, rule[0], _refusal(w, rule[1])
        if facts.card and not facts.room:
            continue
        if facts.orchestrator and not facts.room and facts.own(w):
            continue
        where = "a hosted group room holds no card" if facts.room else (
            f"this session owns no running kanban card assigned to {facts.profile or 'this profile'}")
        return w, "no-card", _refusal(w, where)
    return None


def room_create_refusal(canon: str, facts: Facts, creators: frozenset) -> Optional[str]:
    """``kanban_create`` from a hosted group room: refused unless the profile may start chains there."""
    if canon != KANBAN_CREATE or not facts.room or facts.card or facts.profile in creators:
        return None
    return (_HEAD + "refused kanban_create: a chain does not start from a hosted group room "
            "(chain.card_creator_profiles). Ask in the Slack thread or on the GitLab issue instead; " + NO_CARD + ".")


def _refusal(w: Write, why: str) -> str:
    return (f"{_HEAD}refused {w.label()}: {why}. {NO_CARD}. Reads and edits in your own sandbox stay open; "
            "report the refusal instead of looking for another route.")


def status_post(w: Write, channels) -> bool:
    """The orchestrator's activity / status post: a ``fleet_slack_post`` without ``thread_ts`` to a channel on its own
    ``slack_post`` allow-list, from any of its sessions. The model never picks a thread here; with ``chain_threads``
    the posting tool files it under that chain's own status thread, a root the tool recorded from this profile's own
    earlier post, never someone else's message."""
    return w.kind == "slack" and w.noun == "fleet_slack_post" and not w.thread and w.target in (channels or ())


def _orchestrator_never(w: Write) -> Optional[tuple[str, str]]:
    """What the orchestrator never does, card or not: push, open or edit an MR, write to the forge opaquely."""
    if w.kind == "git-push":
        return "orchestrator-never-pushes", "the orchestrator routes work and never pushes; open a chain card"
    if w.kind != "gitlab":
        return None
    if w.noun == "mr" and w.verb not in ("comment", "reply"):
        return ("orchestrator-never-edits-mrs",
                "the orchestrator never opens or edits a merge request; open a chain card for the builder")
    if w.noun != "issue" and w.noun != "mr":
        return ("orchestrator-opaque-gitlab",
                "the orchestrator makes no forge write the gate cannot read (the imported client, an invocation "
                "the forge CLI's parser refuses, an unknown noun); use the forge CLI")
    return None


def critique_egress(w: Write) -> bool:
    """A write the critique gate holds to a fresh critique: every forge write except ``mr explain``, and, when
    ``critique_push_remotes`` is set, a push to a matching (or unreadable) remote."""
    if w.kind == "gitlab":
        return (w.noun, w.verb) not in _NO_CRITIQUE
    if w.kind == "git-push":
        if not _PUSH_REMOTES:
            return False
        return not w.project or any(r.search("/" + w.project) for r in _PUSH_REMOTES)
    return False


# --------------------------------------------------------------------------- own conversation
def own_slack(w: Write, session: dict) -> bool:
    """A Slack write that answers in the session's own conversation (or the one its cron run delivers to)."""
    if w.kind != "slack":
        return False
    platform, chat, thread = _slack_target(w)
    for p, c, t, user in _conversations(session):
        if platform != "slack" or p != "slack" or not c:
            continue
        same_chat = chat == c or (c.startswith("D") and user and chat == user)
        if same_chat and (thread or "") == (t or ""):
            return True
    return False


def own_gitlab(w: Write, is_own_item: Callable[[str, str, int], bool]) -> bool:
    """A comment on the forge issue or MR whose webhook delivery started this session.

    ``is_own_item(project, kind, iid)`` is ``conversation_origin.is_own_gitlab_item`` (kind ``issue`` /
    ``merge_request``): the item recorded for the session's chat id at ``pre_gateway_dispatch``.
    """
    if w.kind != "gitlab" or (w.noun, w.verb) not in _REPLY_VERBS or w.iid is None or not w.project:
        return False
    return bool(is_own_item(w.project, _ITEM_KIND[w.noun], w.iid))


def _slack_target(w: Write) -> tuple[str, str, str]:
    if w.noun == "fleet_slack_post":
        return "slack", w.target, w.thread
    parts = w.target.split(":")  # send_message: "slack:<chat>[:<thread>]"
    if len(parts) >= 2:
        return parts[0].lower(), parts[1], parts[2] if len(parts) > 2 else ""
    return "", w.target, ""


def _conversations(session: dict) -> list[tuple[str, str, str, str]]:
    out = [(str(session.get("platform") or "").lower(), str(session.get("chat_id") or ""),
            str(session.get("thread_id") or ""), str(session.get("user_id") or ""))]
    cron = session.get("cron_target")
    if isinstance(cron, (tuple, list)) and len(cron) == 3:
        out.append((str(cron[0]).lower(), str(cron[1]), str(cron[2] or ""), ""))
    return out
