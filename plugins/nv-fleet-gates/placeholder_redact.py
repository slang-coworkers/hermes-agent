"""Redact OpenShell credential-placeholder candidates from text bound for the model.

Inside an OpenShell sandbox a credential env var holds only a placeholder, never the real value. The
supervisor proxy scans every outbound REST body for placeholder candidates and refuses a body that carries
one naming a credential not bound to the destination (local HTTP 403
``credential_placeholder_in_request_body``); a malformed candidate fails closed the same way. Every
profile's model call leaves the sandbox, so a tool result or terminal output that carries a live
placeholder (``printenv``, ``git config --list``, ``hermes config get``, a credential helper) kills the
session on the next model call, and keeps killing it while the text stays in the conversation.

``redact`` replaces every candidate the proxy would scan with ``[OpenShell placeholder]``:

* both markers (the canonical prefix and the provider alias marker), plus the env-key characters that
  follow, up to the candidate's end;
* any character percent-encoded (the proxy percent-decodes before it scans; hex digits in either case);
* a marker wrapped across a line (a real newline, or the JSON ``\\n`` escape, optionally after a shell
  ``\\`` continuation and before indentation), so the model never sees enough to reassemble one;
* marker letters in any case. The proxy itself matches the markers case-sensitively; matching more is
  harmless and keeps a re-cased copy from being reassembled.

Both markers are built by concatenation so neither literal appears in this file: a coworker that reads it
would otherwise carry one into its next model call. ``redact`` never raises: on any error it returns the
input unchanged and logs once. A text with no marker word and no ``%`` returns after plain substring scans.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

REPLACEMENT = "[OpenShell placeholder]"

_PREFIX = "openshell:resolve:" + "env:"
_ALIAS = "OPENSHELL-RESOLVE-" + "ENV-"

# A line break inside a marker: optional shell continuation, a real or JSON-escaped newline, indentation.
_WRAP = r"(?:\\?(?:\r?\n|(?:\\r)?\\n)[ \t]*)?"
# The env-key characters that end a candidate (alnum and underscore), literal or percent-encoded.
_KEY_CHAR = r"(?:[A-Za-z0-9_]|%(?:3[0-9]|4[1-9a-f]|5[0-9af]|6[1-9a-f]|7[0-9a]))"


def _char(c: str) -> str:
    return f"(?:{re.escape(c)}|%{ord(c):02x})"


def _marker(marker: str) -> str:
    return _WRAP.join(_char(c) for c in marker)


_MARKERS = f"(?:{_marker(_PREFIX)}|{_marker(_ALIAS)})"
# A key never runs into the next marker: adjacent candidates are separate tokens (as the proxy splits them).
_CANDIDATE = re.compile(f"{_MARKERS}(?:(?!{_MARKERS}){_KEY_CHAR})*", re.IGNORECASE)


def _may_hold_candidate(text: str) -> bool:
    """Cheap gate (plain substring scans): a candidate always leaves "openshell" or "resolve" intact (a single
    wrap splits at most one of the two words) or, when any character is percent-encoded, carries a %."""
    if "%" in text:
        return True
    low = text.lower()
    return "openshell" in low or "resolve" in low


_logged = False


def _log_once() -> None:
    global _logged
    if not _logged:
        _logged = True
        logger.warning("nv-fleet-gates placeholder redaction failed; text passed through unchanged",
                       exc_info=True)


def redact_count(text):
    """``(redacted_text, replacements)``. A non-str, or any error, comes back unchanged with 0."""
    try:
        if not isinstance(text, str) or not text or not _may_hold_candidate(text):
            return text, 0
        return _CANDIDATE.subn(REPLACEMENT, text)
    except Exception:
        logger.debug("placeholder redaction failed", exc_info=True)
        _log_once()
        return text, 0


def redact(text):
    return redact_count(text)[0]
