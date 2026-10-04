"""HTTP client for Gateway-to-Core internal communication.

Every call carries an HMAC-signed user assertion (see ``internal_auth``). The shared
secret itself is never sent on the wire.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union
from urllib.parse import quote, urlencode

import httpx

from plex_playlist_sync.internal_auth import sign_assertion

logger = logging.getLogger(__name__)

# Key under which the gateway attaches its session's creation time (epoch microseconds) to a user dict.
SESSION_ISSUED_AT_KEY = "_session_issued_at_us"

_PATH_SAFE = "/:@+-._~!$&'()*,;=%"


@dataclass
class ProxyResponse:
    """A core response relayed by the gateway: status, selected headers and raw body."""

    status_code: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class CoreClient:
    """Internal HTTP client used by the Gateway tier to call TrackSeerr Core on a user's behalf."""

    def __init__(
        self,
        core_url: str,
        secret: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.core_url = str(core_url).rstrip("/")
        self.secret = secret
        self.timeout = timeout

    @staticmethod
    def _target(path: str, query: Union[None, str, Mapping[str, Any]]) -> str:
        quoted_path = quote(path, safe=_PATH_SAFE)
        if isinstance(query, str):
            qs = query.lstrip("?")
        elif query:
            qs = urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
        else:
            qs = ""
        return f"{quoted_path}?{qs}" if qs else quoted_path

    def _headers(
        self,
        method: str,
        target: str,
        body: bytes = b"",
        user_info: Optional[dict[str, Any]] = None,
        content_type: Optional[str] = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        user_id = str((user_info or {}).get("id") or "")
        user_name = str((user_info or {}).get("username") or "")
        raw_issued = (user_info or {}).get(SESSION_ISSUED_AT_KEY)
        issued_at = int(raw_issued) if raw_issued else None
        headers.update(
            sign_assertion(
                self.secret or "",
                method,
                target,
                user_id,
                user_name,
                body,
                session_issued_at=issued_at,
            )
        )
        return headers

    def _call(
        self,
        method: str,
        path: str,
        query: Union[None, str, Mapping[str, Any]] = None,
        body: bytes = b"",
        user_info: Optional[dict[str, Any]] = None,
        content_type: Optional[str] = None,
    ) -> httpx.Response:
        target = self._target(path, query)
        headers = self._headers(method, target, body, user_info, content_type)
        kwargs: dict[str, Any] = {"headers": headers}
        if body:
            kwargs["content"] = body
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            return client.request(method, f"{self.core_url}{target}", **kwargs)

    def _json_call(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        user_info: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        return self._call(
            method,
            path,
            query,
            body,
            user_info,
            content_type="application/json" if payload is not None else None,
        )

    def get_availability(
        self,
        artist_name: Optional[str] = None,
        album_title: Optional[str] = None,
        track_title: Optional[str] = None,
        foreign_id: Optional[str] = None,
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Queries Core for availability of an artist, album, or track."""
        params: dict[str, str] = {}
        if artist_name:
            params["artist_name"] = artist_name
        if album_title:
            params["album_title"] = album_title
        if track_title:
            params["track_title"] = track_title
        if foreign_id:
            params["foreign_id"] = foreign_id
        resp = self._json_call("GET", "/api/library/availability", query=params, user_info=user_info)
        resp.raise_for_status()
        return resp.json()

    def forward_request(
        self,
        payload: dict[str, Any],
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Forwards single music request creation to TrackSeerr Core as the asserted user."""
        resp = self._json_call("POST", "/api/requests", payload, user_info=user_info)
        resp.raise_for_status()
        return resp.json()

    def forward_batch_requests(
        self,
        payload: dict[str, Any],
        user_info: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Forwards batch music request creation to TrackSeerr Core as the asserted user."""
        resp = self._json_call("POST", "/api/requests/batch", payload, user_info=user_info)
        resp.raise_for_status()
        return resp.json()

    def forward_delete_request(
        self,
        request_id: str,
        user_info: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Forwards deletion of a request to TrackSeerr Core as the asserted user."""
        resp = self._json_call("DELETE", f"/api/requests/{request_id}", user_info=user_info)
        return resp.is_success

    def local_verify(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Asks core to verify local-account credentials, signed as the service principal.

        Returns ``(status_code, json_body)``. The body is ``{}`` when core's reply is not JSON.
        Never logs the payload (it carries the password).
        """
        resp = self._json_call("POST", "/api/internal/auth/local/verify", payload)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}

    def session_status(
        self,
        user_id: str,
        session_issued_at_us: int,
        *,
        record_login: bool = False,
        username: Optional[str] = None,
    ) -> tuple[int, dict[str, Any]]:
        """Asks core whether a gateway session is still allowed, signed as the service principal.

        Returns ``(status_code, json_body)``; the body is ``{}`` when core's reply is not JSON.
        """
        payload: dict[str, Any] = {"user_id": str(user_id), "session_issued_at": int(session_issued_at_us)}
        if record_login:
            payload["record_login"] = True
            payload["username"] = username
        resp = self._json_call("POST", "/api/internal/auth/session-status", payload)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}

    def hello(self) -> tuple[int, dict[str, Any]]:
        """Version/protocol handshake (service principal). Returns ``(status_code, json_body)``."""
        resp = self._json_call("GET", "/api/internal/hello")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}

    def heartbeat(self, payload: dict[str, Any]) -> int:
        """Reports gateway liveness to core (service principal). Returns the HTTP status code."""
        return self._json_call("POST", "/api/internal/gateway-heartbeat", payload).status_code

    def proxy(
        self,
        method: str,
        path: str,
        query: Union[None, str, Mapping[str, Any]],
        body: bytes,
        user_info: Optional[dict[str, Any]],
        content_type: Optional[str] = None,
    ) -> ProxyResponse:
        """Relays an allow-listed user call to core and returns status, Content-Type/Location and body.

        Redirects are never followed so a 3xx ``Location`` reaches the browser untouched.
        """
        resp = self._call(method.upper(), path, query, body or b"", user_info, content_type)
        headers: dict[str, str] = {}
        for name in ("content-type", "location"):
            value = resp.headers.get(name)
            if value:
                headers[name.title()] = value
        return ProxyResponse(status_code=resp.status_code, body=resp.content, headers=headers)
