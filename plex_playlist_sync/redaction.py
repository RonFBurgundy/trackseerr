"""Shared secret redaction for log lines, stored results and API responses.

plexapi and requests exception messages routinely embed full request URLs carrying ``X-Plex-Token=...``;
anything that turns such an exception into text must go through :func:`safe_exc`.
"""

import logging
import re
from typing import Optional

from plexapi.exceptions import BadRequest, NotFound, Unauthorized

_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:x-plex-token|token|apikey|api_key|state|sk|api_sig)=)[^&#\s\"]*"
    # Bare ``X-Plex-Token=abc`` / ``X-Plex-Token: abc`` / ``'X-Plex-Token': 'abc'`` (headers, dict reprs).
    r"|(?<![\w-])(x-plex-token['\"]?\s*[=:]\s*['\"]?)[^&#\s\"',)]+"
)

# Subsonic-API requests carry the credential in the query string (``u``, ``t`` + ``s`` salt, ``p``, ``apiKey``). The
# salted token is replayable, so it is redacted wherever a ``/rest/<endpoint>?...`` URL reaches a log line or response.
_SUBSONIC_URL_RE = re.compile(r"(?i)(/rest/\w+(?:\.view)?\?)([^\s\"'#]*)")
_SUBSONIC_AUTH_PARAM_RE = re.compile(r"(?i)(^|&)(u|t|s|p|apikey)=[^&]*")

# Jellyfin / Emby authorisation header values: ``MediaBrowser Client="..", Token="secret"``.
_MEDIABROWSER_TOKEN_RE = re.compile(r'(?i)(\bToken=")[^"]*(")')

_INVITE_TOKEN_RE = re.compile(r"(/(?:api/auth/)?invite/)[^/?#\s\"']+")

_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@\"']+@")

# Exception types whose message is known not to carry anything beyond a (token-redacted) URL.
_SAFE_BASE_TYPES = (NotFound, BadRequest, Unauthorized)


def _sub_query(match: "re.Match[str]") -> str:
    return (match.group(1) or match.group(2)) + "REDACTED"


def _sub_subsonic_query(match: "re.Match[str]") -> str:
    return match.group(1) + _SUBSONIC_AUTH_PARAM_RE.sub(r"\1\2=REDACTED", match.group(2))


def redact_sensitive_query(text: str) -> str:
    """Replace token/apikey/api_key/state query values, Subsonic auth parameters, and invite/reset link tokens
    (``[REDACTED]``), with ``REDACTED``."""
    text = _SUBSONIC_URL_RE.sub(_sub_subsonic_query, text)
    text = _MEDIABROWSER_TOKEN_RE.sub(r"\1REDACTED\2", text)
    return _INVITE_TOKEN_RE.sub(r"\1[REDACTED]", _SENSITIVE_QUERY_RE.sub(_sub_query, text))


def redact_text(text: str) -> str:
    """:func:`redact_sensitive_query` plus stripping of ``scheme://user:pass@host`` credentials."""
    return _URL_USERINFO_RE.sub(r"\1REDACTED@", redact_sensitive_query(text))


def safe_exc(exc: BaseException, safe_types: Optional[tuple] = None) -> str:
    """Render ``exc`` for logs/results/responses without leaking secrets.

    Always includes the exception type name. The message is appended (redacted) only for plexapi
    ``NotFound``/``BadRequest``/``Unauthorized`` or types listed in ``safe_types`` (callers pass
    application-authored exception types here). Everything else
    (requests errors, generic exceptions) yields the type name only.
    """
    name = type(exc).__name__
    is_safe = isinstance(exc, _SAFE_BASE_TYPES)
    if not is_safe and safe_types:
        is_safe = isinstance(exc, safe_types)
    if not is_safe:
        return name
    message = redact_text(str(exc)).strip()
    return f"{name}: {message}" if message else name


# ---------------------------------------------------------------------------------------------------------------
# Log-line redaction. Broader than :func:`redact_text` (which also guards API responses and stored results): it also
# masks header/dict/JSON ``key: value`` pairs for credentials and bare ``Bearer`` tokens, which only ever matter once
# text is about to hit disk, the ring buffer or stdout.
# ---------------------------------------------------------------------------------------------------------------
_AUTH_HEADER_QUOTED_RE = re.compile(r"""(?i)((?<![A-Za-z0-9])authorization['"]?\s*[:=]\s*)(['"])[^'"\r\n]*\2""")
_AUTH_HEADER_BARE_RE = re.compile(
    r"""(?i)((?<![A-Za-z0-9])authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;)}\]"'\r\n]+"""
)
_SECRET_KEYS = (
    r"x-api-key|x-auth-token|x-plex-token|x-emby-token|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|secret|password|passwd|token"
)
_SECRET_KV_QUOTED_RE = re.compile(
    r"""(?i)((?<![A-Za-z0-9])(?:%s)['"]?\s*[:=]\s*)(['"])[^'"\r\n]*\2""" % _SECRET_KEYS
)
_SECRET_KV_BARE_RE = re.compile(
    r"""(?i)((?<![A-Za-z0-9])(?:%s)\s*[:=]\s*)[^\s,;&)}\]"'\r\n]+""" % _SECRET_KEYS
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def redact_log_text(text: str) -> str:
    """:func:`redact_text` plus credential ``key: value`` pairs (Authorization, X-Api-Key, password, token, ...)."""
    text = redact_text(text)
    text = _AUTH_HEADER_QUOTED_RE.sub(r"\1\2REDACTED\2", text)
    text = _AUTH_HEADER_BARE_RE.sub(r"\1REDACTED", text)
    text = _SECRET_KV_QUOTED_RE.sub(r"\1\2REDACTED\2", text)
    text = _SECRET_KV_BARE_RE.sub(r"\1REDACTED", text)
    return _BEARER_RE.sub(r"\1REDACTED", text)


class RedactLogFilter(logging.Filter):
    """Handler-level filter: renders the record and redacts invite/reset tokens, secret query params and
    credential key/value pairs, in the message, exception text and stack dump."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_log_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        # Exception text and stack dumps are rendered later by Formatter.format; pre-render and redact
        # them here (Formatter reuses a cached ``exc_text``) so tracebacks cannot leak tokens either.
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except (TypeError, ValueError, AttributeError):
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact_log_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_log_text(record.stack_info)
        return True
