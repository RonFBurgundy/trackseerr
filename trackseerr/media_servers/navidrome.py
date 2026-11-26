"""Navidrome native REST API client (admin user discovery and playlist owner transfer).

Navidrome exposes a REST API alongside its Subsonic ``/rest/*`` endpoints:
  * ``POST /auth/login`` ``{username, password}`` -> ``{token, isAdmin, ...}``
  * ``GET /api/user`` (with ``x-nd-authorization: Bearer <token>``) -> list of users (admins see all)
  * ``GET /api/playlist/{id}`` / ``PUT /api/playlist/{id}`` -> read/update playlist including ``ownerId``

Secrets: the password and bearer token are never logged and never placed in exception messages.
"""

from __future__ import annotations

import logging
import time
import weakref
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from trackseerr.media_servers.base import (
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerNotFound,
    MediaServerUnsupported,
    ServerUser,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_MUTATING_RETRY_STATUS = frozenset({429})


class NavidromeAdmin:
    """Thin client for Navidrome's native ``/auth/login`` and ``/api/*`` endpoints."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        transport: Optional[httpx.BaseTransport] = None,
        *,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if base.endswith("/rest"):
            base = base[: -len("/rest")]
        if not base:
            raise MediaServerError("Navidrome URL is not set", safe_detail="Navidrome URL is not set")
        if not username or not password:
            raise MediaServerAuthError(
                "Navidrome needs a username and password",
                safe_detail="Navidrome needs a username and password",
            )
        self._base = base
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep
        self._http = httpx.Client(verify=verify_ssl, timeout=timeout, transport=transport, follow_redirects=False)
        self._finalizer = weakref.finalize(self, self._http.close)
        self._token: Optional[str] = None
        self._is_admin: Optional[bool] = None

    def close(self) -> None:
        self._http.close()

    @property
    def is_admin(self) -> bool:
        """True when the authenticated Navidrome account has administrator privileges."""
        if self._is_admin is None:
            self.login()
        return bool(self._is_admin)

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        delay = _BACKOFF_BASE * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = max(delay, float(retry_after))
        return min(delay, _BACKOFF_CAP)

    @staticmethod
    def _transport_error(exc: httpx.HTTPError) -> MediaServerConnectionError:
        detail = f"Navidrome server unreachable ({type(exc).__name__})"
        return MediaServerConnectionError(detail, safe_detail=detail)

    @staticmethod
    def _check_status(path: str, response: httpx.Response) -> None:
        status = response.status_code
        if status in (401, 403):
            detail = f"Navidrome rejected the credentials (HTTP {status})"
            raise MediaServerAuthError("Navidrome rejected the credentials", safe_detail=detail)
        if status >= 500 or status == 429:
            detail = f"Navidrome server error (HTTP {status})"
            raise MediaServerConnectionError(detail, safe_detail=detail)
        if status == 404:
            detail = f"Navidrome {path} not found (HTTP 404)"
            raise MediaServerNotFound(detail, safe_detail=detail)
        if status >= 400:
            detail = f"Navidrome {path} failed (HTTP {status})"
            raise MediaServerError(detail, safe_detail=detail)

    @staticmethod
    def _parse_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            detail = "Navidrome server returned an unexpected response"
            raise MediaServerError(detail, safe_detail=detail) from exc

    def _send_with_retry(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Any = None,
        mutating: bool = False,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        retry_status = _MUTATING_RETRY_STATUS if mutating else _RETRY_STATUS
        response: Optional[httpx.Response] = None
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                response = self._http.request(method, url, headers=headers, json=json_body)
            except httpx.HTTPError as exc:
                if mutating or last:
                    raise self._transport_error(exc) from exc
                logger.debug("Navidrome %s %s transport failure (%s); retrying", method, path, type(exc).__name__)
                self._sleep(self._backoff(attempt, None))
                continue
            if response.status_code in retry_status and not last:
                logger.warning("Navidrome %s %s answered HTTP %s; retrying", method, path, response.status_code)
                self._sleep(self._backoff(attempt, response))
                continue
            break
        assert response is not None
        return response

    def login(self, *, force: bool = False) -> str:
        """Authenticate against ``POST /auth/login``, caching the bearer token and admin flag."""
        if self._token is not None and not force:
            return self._token
        response = self._send_with_retry(
            "POST",
            "/auth/login",
            json_body={"username": self._username, "password": self._password},
            mutating=False,
        )
        self._check_status("/auth/login", response)
        payload = self._parse_json(response)
        if not isinstance(payload, dict) or not payload.get("token"):
            detail = "Navidrome login returned an unexpected response"
            raise MediaServerError(detail, safe_detail=detail)
        self._token = str(payload["token"])
        self._is_admin = bool(payload.get("isAdmin", False))
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        mutating: bool = False,
    ) -> Any:
        token = self.login()
        response = self._send_with_retry(
            method,
            path,
            headers={"x-nd-authorization": f"Bearer {token}"},
            json_body=json_body,
            mutating=mutating,
        )
        if response.status_code == 401:
            token = self.login(force=True)
            response = self._send_with_retry(
                method,
                path,
                headers={"x-nd-authorization": f"Bearer {token}"},
                json_body=json_body,
                mutating=mutating,
            )
        self._check_status(path, response)
        return self._parse_json(response)

    def list_users(self) -> list[ServerUser]:
        """List all Navidrome users (admin only)."""
        if not self.is_admin:
            detail = "Navidrome account is not an administrator"
            raise MediaServerUnsupported(detail, safe_detail=detail)
        payload = self._request("GET", "/api/user")
        if not isinstance(payload, list):
            detail = "Navidrome /api/user returned an unexpected response"
            raise MediaServerError(detail, safe_detail=detail)
        users: list[ServerUser] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            uid = str(raw.get("id") or "").strip()
            uname = str(raw.get("userName") or "").strip()
            if not uid or not uname:
                continue
            users.append(
                ServerUser(
                    id=uid,
                    name=uname,
                    is_admin=bool(raw.get("isAdmin", False)),
                    extra={"email": str(raw.get("email") or "").strip()},
                )
            )
        return users

    def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        """Read a single Navidrome playlist object via ``GET /api/playlist/{id}``."""
        encoded = quote(str(playlist_id), safe="")
        payload = self._request("GET", f"/api/playlist/{encoded}")
        if not isinstance(payload, dict):
            detail = "Navidrome /api/playlist returned an unexpected response"
            raise MediaServerError(detail, safe_detail=detail)
        return payload

    def set_owner(self, playlist_id: str, owner_id: str) -> bool:
        """Transfer ownership of ``playlist_id`` to ``owner_id`` (GET, PUT editable fields, then GET to verify)."""
        current = self.get_playlist(playlist_id)
        body: dict[str, Any] = {
            "name": current.get("name"),
            "comment": current.get("comment"),
            "public": current.get("public"),
            "ownerId": str(owner_id),
        }
        if current.get("rules"):
            body["rules"] = current["rules"]
        encoded = quote(str(playlist_id), safe="")
        self._request(
            "PUT",
            f"/api/playlist/{encoded}",
            json_body=body,
            mutating=True,
        )
        updated = self.get_playlist(playlist_id)
        return str(updated.get("ownerId") or "") == str(owner_id)
