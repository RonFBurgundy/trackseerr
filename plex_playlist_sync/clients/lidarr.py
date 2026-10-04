"""Lidarr REST API Client for automated music discovery and library queuing."""

import logging
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx

from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)


def _exc_text(exc: BaseException) -> str:
    """Secret-safe exception text: httpx/Lidarr errors keep their (redacted) message, anything else the type name."""
    return safe_exc(exc, safe_types=(httpx.HTTPError, LidarrApiError))


class LidarrApiError(Exception):
    """A Lidarr request failed. The message is application-authored and never carries the API key."""


class LidarrClient:
    """Client for interacting with Lidarr's REST API (v1)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        auto_search: bool = True,
        root_folder: Optional[str] = None,
        quality_profile_id: Optional[int] = None,
        metadata_profile_id: Optional[int] = None,
        timeout: float = 15.0,
        monitor_option: Optional[str] = None,
        tag_ids: Optional[list[int]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.verify_ssl = verify_ssl
        self.auto_search = auto_search
        self.root_folder = root_folder
        self.quality_profile_id = quality_profile_id
        self.metadata_profile_id = metadata_profile_id
        self.timeout = timeout
        self.monitor_option = monitor_option
        self.tag_ids = list(tag_ids or [])

    def _get_json(self, path: str) -> Any:
        """GET ``/api/v1/<path>`` with the bounded client timeout; raises LidarrApiError on any failure."""
        url = f"{self.base_url}/api/v1/{path}"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                resp = client.get(url, headers=self._get_headers())
        except httpx.HTTPError as exc:
            raise LidarrApiError(f"Could not reach Lidarr ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise LidarrApiError("Lidarr rejected the API key")
        if resp.status_code != 200:
            raise LidarrApiError(f"Lidarr returned HTTP {resp.status_code} for {path}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LidarrApiError(f"Lidarr returned invalid JSON for {path}") from exc

    def get_options(self) -> dict[str, list[dict[str, Any]]]:
        """Live root folders, quality profiles, metadata profiles and tags (for the settings pickers)."""
        roots = self._get_json("rootfolder")
        quality = self._get_json("qualityprofile")
        metadata = self._get_json("metadataprofile")
        tags = self._get_json("tag")
        if not all(isinstance(v, list) for v in (roots, quality, metadata, tags)):
            raise LidarrApiError("Lidarr returned an unexpected response shape")
        return {
            "root_folders": [
                {"path": str(r.get("path", "")), "free_space": int(r.get("freeSpace") or 0)}
                for r in roots
                if isinstance(r, dict)
            ],
            "quality_profiles": [
                {"id": int(q["id"]), "name": str(q.get("name", ""))} for q in quality if isinstance(q, dict) and "id" in q
            ],
            "metadata_profiles": [
                {"id": int(m["id"]), "name": str(m.get("name", ""))} for m in metadata if isinstance(m, dict) and "id" in m
            ],
            "tags": [{"id": int(t["id"]), "label": str(t.get("label", ""))} for t in tags if isinstance(t, dict) and "id" in t],
        }

    def get_health(self) -> list[dict[str, Any]]:
        """Lidarr's ``/health`` checks, normalised to ``{source, type, message, wiki_url}``."""
        data = self._get_json("health")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected health response")
        allowed = {"ok", "notice", "warning", "error"}
        out: list[dict[str, Any]] = []
        for h in data:
            if not isinstance(h, dict):
                continue
            kind = str(h.get("type", "")).lower()
            out.append(
                {
                    "source": str(h.get("source", "")),
                    "type": kind if kind in allowed else "notice",
                    "message": str(h.get("message", "")),
                    "wiki_url": h.get("wikiUrl") or None,
                }
            )
        return out

    def _get_headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def test_connection(self) -> dict[str, Any]:
        """Validates connectivity and authentication against Lidarr."""
        url = f"{self.base_url}/api/v1/system/status"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                resp = client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "online": True,
                        "version": data.get("version", "unknown"),
                        "app_name": data.get("appName", "Lidarr"),
                    }
                return {
                    "online": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:100]}",
                }
        except Exception as e:
            return {"online": False, "error": _exc_text(e)}

    def get_root_folder(self, client: Optional[httpx.Client] = None) -> str:
        """Retrieves configured or default Lidarr root folder path."""
        if self.root_folder:
            return self.root_folder
        url = f"{self.base_url}/api/v1/rootfolder"
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    return str(data[0].get("path", "/music"))
        except Exception as e:
            logger.warning("Could not discover Lidarr root folder: %s", _exc_text(e))
        return "/music"

    def get_quality_profile_id(self, client: Optional[httpx.Client] = None) -> int:
        """Retrieves configured or default Lidarr quality profile ID."""
        if self.quality_profile_id is not None:
            return self.quality_profile_id
        url = f"{self.base_url}/api/v1/qualityprofile"
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    return int(data[0].get("id", 1))
        except Exception as e:
            logger.warning("Could not discover Lidarr quality profile: %s", _exc_text(e))
        return 1

    def get_metadata_profile_id(self, client: Optional[httpx.Client] = None) -> int:
        """Retrieves configured or default Lidarr metadata profile ID."""
        if self.metadata_profile_id is not None:
            return self.metadata_profile_id
        url = f"{self.base_url}/api/v1/metadataprofile"
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    return int(data[0].get("id", 1))
        except Exception as e:
            logger.warning("Could not discover Lidarr metadata profile: %s", _exc_text(e))
        return 1

    def add_artist_and_albums(
        self,
        artist_name: str,
        album_names: Optional[list[str]] = None,
        auto_search: Optional[bool] = None,
        monitor_mode: str = "specific",
    ) -> dict[str, Any]:
        """Looks up an artist, ensures the artist and requested albums are monitored in Lidarr.

        Optimized for bulk onboarding:
        - 1 artist lookup per artist (drastically reduces MusicBrainz network calls)
        - Supports monitor_mode="specific" to prevent downloading full artist discographies
        - Batches album search commands into a single AlbumSearch call
        - Gracefully handles HTTP 429 / 503 rate limits with retry-after guidance
        """
        clean_artist = artist_name.strip()
        if not clean_artist:
            return {"status": "error", "message": "Empty artist name"}

        clean_albums = [a.strip() for a in (album_names or []) if a and a.strip()]
        should_search = self.auto_search if auto_search is None else auto_search
        headers = self._get_headers()

        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                # 1. Look up artist in Lidarr / MusicBrainz
                lookup_url = f"{self.base_url}/api/v1/artist/lookup?term={quote(clean_artist)}"
                resp = client.get(lookup_url, headers=headers)

                if resp.status_code in (429, 503):
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    return {
                        "status": "rate_limited",
                        "artist": clean_artist,
                        "retry_after": retry_after,
                        "message": f"Rate limited by Lidarr/MusicBrainz (HTTP {resp.status_code}). Backing off for {retry_after}s.",
                    }

                if resp.status_code != 200:
                    return {
                        "status": "error",
                        "artist": clean_artist,
                        "message": f"Lookup failed: HTTP {resp.status_code}",
                    }

                results = resp.json()
                if not results or not isinstance(results, list):
                    return {
                        "status": "not_found",
                        "artist": clean_artist,
                        "message": "Artist not found in Lidarr lookup",
                    }

                candidate = results[0]
                artist_id = candidate.get("id", 0)
                artist_title = candidate.get("artistName", clean_artist)
                was_new = False

                # 2. If artist is not yet in library
                if not artist_id:
                    root_folder = self.get_root_folder(client)
                    quality_id = self.get_quality_profile_id(client)
                    metadata_id = self.get_metadata_profile_id(client)

                    if self.monitor_option:
                        add_monitor = self.monitor_option
                    else:
                        add_monitor = "none" if monitor_mode == "specific" else "all"
                    payload = {
                        **candidate,
                        "monitored": True,
                        "rootFolderPath": root_folder,
                        "qualityProfileId": quality_id,
                        "metadataProfileId": metadata_id,
                        "tags": list(self.tag_ids),
                        "addOptions": {
                            "monitor": add_monitor,
                            "searchForMissingAlbums": False,
                        },
                    }

                    add_url = f"{self.base_url}/api/v1/artist"
                    add_resp = client.post(add_url, headers=headers, json=payload)
                    if add_resp.status_code in (429, 503):
                        retry_after = int(add_resp.headers.get("Retry-After", 60))
                        return {
                            "status": "rate_limited",
                            "artist": artist_title,
                            "retry_after": retry_after,
                            "message": f"Rate limited during artist add (HTTP {add_resp.status_code}).",
                        }

                    if add_resp.status_code in (200, 201):
                        added_data = add_resp.json()
                        artist_id = added_data.get("id", 0)
                        was_new = True
                        logger.info("Added artist '%s' (ID %s) to Lidarr", artist_title, artist_id)
                    else:
                        return {
                            "status": "error",
                            "artist": artist_title,
                            "message": f"Failed to add artist: HTTP {add_resp.status_code}",
                        }

                # 3. Locate requested albums and ensure they are monitored
                matched_album_ids: list[int] = []
                if artist_id and clean_albums:
                    try:
                        alb_url = f"{self.base_url}/api/v1/album?artistId={artist_id}"
                        alb_resp = client.get(alb_url, headers=headers)
                        if alb_resp.status_code in (429, 503):
                            return {
                                "status": "rate_limited",
                                "artist": artist_title,
                                "retry_after": int(alb_resp.headers.get("Retry-After", 60)),
                                "message": "Rate limited while fetching albums.",
                            }
                        if alb_resp.status_code == 200:
                            albums = alb_resp.json()
                            for req_alb in clean_albums:
                                req_lower = req_alb.lower()
                                for alb in albums:
                                    alb_title = (alb.get("title") or "").lower()
                                    if req_lower in alb_title or alb_title in req_lower:
                                        a_id = alb.get("id")
                                        if a_id and a_id not in matched_album_ids:
                                            matched_album_ids.append(a_id)
                                            if not alb.get("monitored"):
                                                alb["monitored"] = True
                                                client.put(f"{self.base_url}/api/v1/album/{a_id}", headers=headers, json=alb)
                                        break
                    except Exception as e:
                        logger.warning("Error inspecting Lidarr albums for artist %s: %s", artist_id, _exc_text(e))

                # 4. Trigger decoupled search command if requested
                searched = False
                if should_search:
                    cmd_url = f"{self.base_url}/api/v1/command"
                    if matched_album_ids:
                        cmd_payload = {"name": "AlbumSearch", "albumIds": matched_album_ids}
                        cmd_resp = client.post(cmd_url, headers=headers, json=cmd_payload)
                        searched = cmd_resp.status_code in (200, 201)
                    elif was_new and monitor_mode != "specific":
                        cmd_payload = {"name": "ArtistSearch", "artistId": artist_id}
                        cmd_resp = client.post(cmd_url, headers=headers, json=cmd_payload)
                        searched = cmd_resp.status_code in (200, 201)

                return {
                    "status": "success",
                    "artist": artist_title,
                    "artist_id": artist_id,
                    "added": was_new,
                    "matched_album_ids": matched_album_ids,
                    "searched": searched,
                    "message": f"{'Added and monitored' if was_new else 'Monitored'} in Lidarr ({len(matched_album_ids)} album(s))",
                }

        except Exception as e:
            logger.error("Exception in Lidarr add_artist_and_albums: %s", _exc_text(e))
            return {"status": "error", "artist": clean_artist, "message": _exc_text(e)}

    def search_and_add_track(
        self,
        artist_name: str,
        album_name: str = "",
        title: str = "",
        auto_search: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Finds artist/album in Lidarr, ensures it is monitored, and triggers search."""
        res = self.add_artist_and_albums(
            artist_name=artist_name,
            album_names=[album_name] if album_name else [],
            auto_search=auto_search,
            monitor_mode="specific",
        )
        if res.get("status") == "success":
            return {
                "status": "added" if res.get("added") else "already_monitored",
                "artist": res.get("artist", artist_name),
                "album": album_name,
                "lidarr_id": res.get("artist_id", 0),
                "searched": res.get("searched", False),
                "message": res.get("message", ""),
            }
        return res

    def get_all_artists(self, client: Optional[httpx.Client] = None) -> list[dict[str, Any]]:
        """Retrieves all artists monitored or unmonitored from Lidarr."""
        url = f"{self.base_url}/api/v1/artist"
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr artists: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching artists from Lidarr: %s", _exc_text(e))
        return []

    def get_all_albums(
        self,
        artist_id: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ) -> list[dict[str, Any]]:
        """Retrieves albums from Lidarr, optionally filtered by artist ID."""
        url = f"{self.base_url}/api/v1/album"
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers, params=params if params else None)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers, params=params if params else None)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr albums: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching albums from Lidarr: %s", _exc_text(e))
        return []

    def get_all_tracks(
        self,
        artist_id: Optional[int] = None,
        album_id: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ) -> list[dict[str, Any]]:
        """Retrieves tracks from Lidarr, optionally filtered by artistId and/or albumId."""
        url = f"{self.base_url}/api/v1/track"
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        if album_id is not None:
            params["albumId"] = album_id
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers, params=params if params else None)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers, params=params if params else None)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr tracks: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching tracks from Lidarr: %s", _exc_text(e))
        return []

    def get_all_track_files(
        self,
        artist_id: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ) -> list[dict[str, Any]]:
        """Retrieves physical track files from Lidarr, optionally filtered by artist ID."""
        url = f"{self.base_url}/api/v1/trackfile"
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers, params=params if params else None)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers, params=params if params else None)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr track files: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching track files from Lidarr: %s", _exc_text(e))
        return []

    # ------------------------------------------------------------------ Activity / Wanted (admin proxy)

    def _send_json(self, method: str, path: str, json_body: Optional[dict[str, Any]] = None) -> Any:
        """POST or DELETE ``/api/v1/<path>``; returns the parsed JSON body (``None`` when empty).

        Raises LidarrApiError on any failure, with a message that never carries the API key.
        """
        url = f"{self.base_url}/api/v1/{path}"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                if method == "POST":
                    resp = client.post(url, headers=self._get_headers(), json=json_body)
                elif method == "DELETE":
                    resp = client.delete(url, headers=self._get_headers())
                else:
                    raise ValueError(f"Unsupported method: {method}")
        except httpx.HTTPError as exc:
            raise LidarrApiError(f"Could not reach Lidarr ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise LidarrApiError("Lidarr rejected the API key")
        if resp.status_code == 404:
            raise LidarrApiError(f"Lidarr could not find the item for {path}")
        if resp.status_code not in (200, 201, 202, 204):
            raise LidarrApiError(f"Lidarr returned HTTP {resp.status_code} for {path}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _get_page(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET a paged Lidarr resource and check it has the ``{page, pageSize, totalRecords, records}`` shape."""
        data = self._get_json(f"{path}?{urlencode(params)}")
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise LidarrApiError(f"Lidarr returned an unexpected response shape for {path}")
        return data

    @staticmethod
    def _page_params(page: int, page_size: int, sort_key: str, sort_dir: str, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": int(page),
            "pageSize": int(page_size),
            "sortKey": sort_key,
            "sortDirection": "descending" if str(sort_dir).lower() == "desc" else "ascending",
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def get_queue(self, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
        return self._get_page(
            "queue",
            self._page_params(page, page_size, sort_key, sort_dir, includeArtist="true", includeAlbum="true"),
        )

    def delete_queue_item(self, queue_id: int, remove_from_client: bool, blocklist: bool) -> None:
        query = urlencode(
            {"removeFromClient": str(bool(remove_from_client)).lower(), "blocklist": str(bool(blocklist)).lower()}
        )
        self._send_json("DELETE", f"queue/{int(queue_id)}?{query}")

    def get_history(
        self, page: int, page_size: int, sort_key: str, sort_dir: str, event_type: Optional[int] = None
    ) -> dict[str, Any]:
        return self._get_page(
            "history",
            self._page_params(
                page, page_size, sort_key, sort_dir, includeArtist="true", includeAlbum="true", eventType=event_type
            ),
        )

    def mark_history_failed(self, history_id: int) -> None:
        self._send_json("POST", f"history/failed/{int(history_id)}")

    def get_blocklist(self, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
        return self._get_page("blocklist", self._page_params(page, page_size, sort_key, sort_dir))

    def delete_blocklist_item(self, blocklist_id: int) -> None:
        self._send_json("DELETE", f"blocklist/{int(blocklist_id)}")

    def get_wanted(self, kind: str, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
        """``kind`` is ``missing`` or ``cutoff``."""
        if kind not in ("missing", "cutoff"):
            raise ValueError(f"Unknown wanted list: {kind!r}")
        return self._get_page(
            f"wanted/{kind}",
            self._page_params(page, page_size, sort_key, sort_dir, includeArtist="true", monitored="true"),
        )

    def run_command(self, name: str, **body: Any) -> dict[str, Any]:
        """POST ``/command`` (e.g. ``AlbumSearch`` with ``albumIds``); returns Lidarr's command resource."""
        result = self._send_json("POST", "command", {"name": name, **body})
        return result if isinstance(result, dict) else {}
