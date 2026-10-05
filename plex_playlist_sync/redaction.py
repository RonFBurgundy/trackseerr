"""Shared secret redaction for log lines, stored results and API responses.

plexapi and requests exception messages routinely embed full request URLs carrying ``X-Plex-Token=...``;
anything that turns such an exception into text must go through :func:`safe_exc`.
"""

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
