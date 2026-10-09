"""Outbound scrobbling clients for Last.fm and ListenBrainz.

Both talk to fixed HTTPS endpoints (no user-influenced URLs), retry with backoff
on 429/502/503/504 and log every failure with its root cause.
"""

import hashlib
import logging
import time
from typing import Any, Mapping, Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

LASTFM_AUTH_URL = "https://www.last.fm/api/auth/"
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LISTENBRAINZ_API_URL = "https://api.listenbrainz.org"

RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 10.0

# Last.fm: invalid session (9) and bad/expired/unauthorized token (4, 14, 15) never succeed on retry.
LASTFM_PERMANENT_CODES = frozenset({4, 9, 14, 15})
LASTFM_INVALID_SESSION = 9


def _retry_delay(attempt: int, response: Optional[requests.Response]) -> float:
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                delay = max(delay, min(float(raw), MAX_RETRY_AFTER_SECONDS))
            except ValueError:
                logger.debug("Ignoring non-numeric Retry-After header: %r", raw)
    return delay


def lastfm_signature(params: Mapping[str, Any], secret: str) -> str:
    """Last.fm api_sig: sort params (excluding ``format`` and ``callback``) by key,
    concatenate ``key+value``, append the secret and md5 hexdigest the result."""
    base = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("format", "callback"))
    return hashlib.md5((base + secret).encode("utf-8")).hexdigest()  # noqa: S324 - mandated by the Last.fm API


class LastFmError(Exception):
    """Raised on Last.fm failures. ``code`` is the Last.fm error number, or 0 for transport/HTTP failures."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Last.fm error {code}: {message}")
        self.code = code
        self.message = message

    @property
    def permanent(self) -> bool:
        return self.code in LASTFM_PERMANENT_CODES

    @property
    def invalid_session(self) -> bool:
        return self.code == LASTFM_INVALID_SESSION


class LastFmClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        session: Optional[requests.Session] = None,
        timeout: int = 10,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session or requests.Session()
        self.timeout = timeout

    def build_auth_url(self, callback_url: str) -> str:
        return f"{LASTFM_AUTH_URL}?api_key={quote_plus(self.api_key)}&cb={quote_plus(callback_url)}"

    def _call(self, method: str, params: dict[str, Any], http_method: str = "POST") -> dict[str, Any]:
        full: dict[str, Any] = {k: v for k, v in params.items() if v is not None}
        full["method"] = method
        full["api_key"] = self.api_key
        full["api_sig"] = lastfm_signature(full, self.api_secret)
        full["format"] = "json"

        last_error: Optional[LastFmError] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response: Optional[requests.Response] = None
            try:
                if http_method == "GET":
                    response = self.session.get(LASTFM_API_URL, params=full, timeout=self.timeout)
                else:
                    response = self.session.post(LASTFM_API_URL, data=full, timeout=self.timeout)
            except requests.RequestException as exc:
                # Exception text can embed the request URL (token, api_sig, sk): log the type only.
                logger.warning(
                    "Last.fm %s request failed (attempt %d/%d): %s",
                    method, attempt, MAX_ATTEMPTS, type(exc).__name__,
                )
                last_error = LastFmError(0, f"network error ({type(exc).__name__})")
            else:
                if response.status_code in RETRY_STATUSES:
                    logger.warning(
                        "Last.fm %s returned HTTP %d (attempt %d/%d)",
                        method, response.status_code, attempt, MAX_ATTEMPTS,
                    )
                    last_error = LastFmError(0, f"HTTP {response.status_code}")
                else:
                    return self._parse(method, response)
            if attempt < MAX_ATTEMPTS:
                time.sleep(_retry_delay(attempt, response))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse(method: str, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error(
                "Last.fm %s returned non-JSON body (HTTP %d): %s", method, response.status_code, type(exc).__name__
            )
            raise LastFmError(0, f"invalid JSON response (HTTP {response.status_code})") from exc
        if isinstance(payload, dict) and "error" in payload:
            try:
                code = int(payload["error"])
            except (TypeError, ValueError):
                code = 0
            message = str(payload.get("message", "unknown error"))
            logger.warning("Last.fm %s failed with error %d: %s", method, code, message)
            raise LastFmError(code, message)
        if response.status_code >= 400:
            raise LastFmError(0, f"HTTP {response.status_code}")
        if not isinstance(payload, dict):
            raise LastFmError(0, "unexpected response shape")
        return payload

    def exchange_token_for_session(self, token: str) -> tuple[str, str]:
        """auth.getSession: returns ``(username, session_key)``."""
        payload = self._call("auth.getSession", {"token": token}, http_method="POST")
        sess = payload.get("session")
        if not isinstance(sess, dict) or not sess.get("key") or not sess.get("name"):
            raise LastFmError(0, "auth.getSession response missing session")
        return str(sess["name"]), str(sess["key"])

    def scrobble(
        self,
        artist: str,
        track: str,
        timestamp: int,
        session_key: str,
        album: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._call(
            "track.scrobble",
            {"artist": artist, "track": track, "timestamp": int(timestamp), "album": album, "sk": session_key},
        )

    def now_playing(
        self,
        artist: str,
        track: str,
        session_key: str,
        album: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict[str, Any]:
        return self._call(
            "track.updateNowPlaying",
            {"artist": artist, "track": track, "album": album, "duration": duration, "sk": session_key},
        )


class ListenBrainzError(Exception):
    """Raised on ListenBrainz failures. ``status`` is the HTTP status, or 0 for transport failures."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"ListenBrainz error {status}: {message}")
        self.status = status
        self.message = message

    @property
    def permanent(self) -> bool:
        return self.status in (400, 401, 403)


class ListenBrainzClient:
    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 10) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, http_method: str, path: str, token: str, **kwargs: Any) -> requests.Response:
        url = f"{LISTENBRAINZ_API_URL}{path}"
        headers = {"Authorization": f"Token {token}"}
        last_error: Optional[ListenBrainzError] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response: Optional[requests.Response] = None
            try:
                response = self.session.request(
                    http_method, url, headers=headers, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                logger.warning(
                    "ListenBrainz %s failed (attempt %d/%d): %s", path, attempt, MAX_ATTEMPTS, type(exc).__name__
                )
                last_error = ListenBrainzError(0, f"network error ({type(exc).__name__})")
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                logger.warning(
                    "ListenBrainz %s returned HTTP %d (attempt %d/%d)",
                    path, response.status_code, attempt, MAX_ATTEMPTS,
                )
                last_error = ListenBrainzError(response.status_code, f"HTTP {response.status_code}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(_retry_delay(attempt, response))
        assert last_error is not None
        raise last_error

    def submit_listen(
        self,
        token: str,
        artist: str,
        track: str,
        release: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> None:
        metadata: dict[str, Any] = {"artist_name": artist, "track_name": track}
        if release:
            metadata["release_name"] = release
        entry: dict[str, Any] = {"track_metadata": metadata}
        listen_type = "single"
        if timestamp is not None:
            entry["listened_at"] = int(timestamp)
        else:
            listen_type = "playing_now"
        response = self._request(
            "POST", "/1/submit-listens", token, json={"listen_type": listen_type, "payload": [entry]}
        )
        if response.status_code != 200:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error", "")) if isinstance(body, dict) else ""
            except ValueError:
                detail = response.text[:200]
            logger.warning("ListenBrainz submit-listens rejected (HTTP %d): %s", response.status_code, detail)
            raise ListenBrainzError(response.status_code, detail or f"HTTP {response.status_code}")

    def validate_token(self, token: str) -> Optional[str]:
        """Return the ListenBrainz username for a valid token, ``None`` for an invalid one."""
        response = self._request("GET", "/1/validate-token", token)
        if response.status_code == 401:
            return None
        if response.status_code != 200:
            raise ListenBrainzError(response.status_code, f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ListenBrainzError(response.status_code, "invalid JSON response") from exc
        if isinstance(body, dict) and body.get("valid"):
            return str(body.get("user_name") or "") or None
        return None
