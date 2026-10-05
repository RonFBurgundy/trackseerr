"""Fixtures for the Lidarr contract tests: a REAL Lidarr (no download clients, no indexers), see docs/INTEGRATION_TESTS.md.

Only collected with ``RUN_INTEGRATION=1`` (see ``tests/conftest.py``).

Target selection, in order:
  1. ``LIDARR_IT_URL`` + ``LIDARR_IT_API_KEY`` (an already-running throwaway Lidarr);
  2. the compose stack's default (``http://127.0.0.1:18686`` and the test-only key), already running;
  3. ``docker compose -f docker-compose.integration.yml up -d`` (only when the ``docker`` CLI is present); that stack
     is torn down (``down -v``) at the end of the session. A stack found already running is left alone.

WARNING: the fixtures create/overwrite the ``/music`` and ``/music-nosingles`` root folders, the ``IT-*`` metadata
profiles and the ``it-tag`` tag, and delete the test artist. Point ``LIDARR_IT_URL`` only at a throwaway instance; the
session is skipped when the instance already holds artists other than the test artist.
"""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from plex_playlist_sync.clients import lidarr as lidarr_mod
from plex_playlist_sync.clients.lidarr import LidarrClient, invalidate_add_defaults

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.integration.yml"
DEFAULT_URL = "http://127.0.0.1:18686"
# TEST-ONLY key, seeded in tests/integration/lidarr/config.xml. Not a secret.
DEFAULT_API_KEY = "a5ef0acebccb7d5f1a894d9ed1b96e98"

# Neutral Milk Hotel: a finished band (the MusicBrainz catalogue does not grow), seven releases under a
# singles-allowed profile: 2 studio albums, 1 EP, 4 singles. "Holland, 1945" is both on the album "In the Aeroplane
# Over the Sea" and on the single "Holland, 1945"; "Unborn" exists only on the single "Everything Is".
ARTIST_NAME = "Neutral Milk Hotel"
ARTIST_MBID = "a506f761-2c22-4b2f-8a94-bd748c2c8f75"
SONG_ON_SINGLE_AND_ALBUM = "Holland, 1945"
SONG_ONLY_ON_A_SINGLE = "Unborn"

TAG_LABEL = "it-tag"
PROFILE_WITH_SINGLES = "IT-WithSingles"
PROFILE_NO_SINGLES = "IT-NoSingles"
ROOT_MAIN = "/music"
ROOT_NO_SINGLES = "/music-nosingles"
MONITOR_OPTION = "future"
NEW_ITEM_OPTION = "new"
QUALITY_NAME = "Lossless"  # a stock Lidarr quality profile; not the first one, so a "first profile" fallback would differ


class LidarrAdmin:
    """Thin raw httpx wrapper for fixtures and assertions (never goes through the code under test)."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(headers={"X-Api-Key": api_key}, timeout=60.0)

    def url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path}"

    def get(self, path: str) -> Any:
        resp = self._http.get(self.url(path))
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: Any) -> Any:
        resp = self._http.post(self.url(path), json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def put(self, path: str, body: Any) -> Any:
        resp = self._http.put(self.url(path), json=body)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def delete(self, path: str) -> None:
        self._http.delete(self.url(path)).raise_for_status()

    def artists(self) -> list[dict[str, Any]]:
        return self.get("artist")

    def delete_test_artist(self) -> None:
        for artist in self.artists():
            if artist.get("foreignArtistId") == ARTIST_MBID:
                self.delete(f"artist/{artist['id']}?deleteFiles=false&addImportListExclusion=false")

    def test_artist(self) -> Optional[dict[str, Any]]:
        return next((a for a in self.artists() if a.get("foreignArtistId") == ARTIST_MBID), None)

    def albums(self, artist_id: int) -> list[dict[str, Any]]:
        return self.get(f"album?artistId={artist_id}")

    def monitored_album_ids(self, artist_id: int) -> list[int]:
        return sorted(a["id"] for a in self.albums(artist_id) if a.get("monitored"))

    def wait_add_options_consumed(self, artist_id: int, seconds: float = 60.0) -> None:
        """Lidarr applies ``addOptions.monitor`` shortly after RefreshArtist reports completed, then sets
        ``addOptions`` to null. Until then every album of a new artist lists as monitored (see docs/INTEGRATION_TESTS.md)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.get(f"artist/{artist_id}").get("addOptions") is None:
                return
            time.sleep(0.1)
        raise AssertionError(f"Lidarr never applied the add options of artist {artist_id}")

    def unmonitor_all(self, artist_id: int) -> None:
        ids = [a["id"] for a in self.albums(artist_id)]
        if ids:
            self.put("album/monitor", {"albumIds": ids, "monitored": False})


def _reachable(url: str, key: str) -> bool:
    try:
        return httpx.get(f"{url}/api/v1/system/status", headers={"X-Api-Key": key}, timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def _wait_ready(url: str, key: str, seconds: float = 180.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _reachable(url, key):
            return True
        time.sleep(2.0)
    return False


@pytest.fixture(scope="session")
def lidarr_target():
    """``(url, api_key)`` of a ready Lidarr; starts the compose stack when none is running."""
    url = os.environ.get("LIDARR_IT_URL")
    key = os.environ.get("LIDARR_IT_API_KEY")
    if url:
        if not key:
            pytest.skip("LIDARR_IT_URL is set but LIDARR_IT_API_KEY is not")
        if not _wait_ready(url.rstrip("/"), key, 60.0):
            pytest.skip(f"Lidarr at {url} did not answer /api/v1/system/status")
        yield url.rstrip("/"), key
        return
    if _reachable(DEFAULT_URL, DEFAULT_API_KEY):
        yield DEFAULT_URL, DEFAULT_API_KEY
        return
    if shutil.which("docker") is None:
        pytest.skip("no Lidarr reachable and no docker CLI: run `docker compose -f docker-compose.integration.yml up -d`")
    started = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "lidarr"], capture_output=True, text=True, timeout=600
    )
    if started.returncode != 0:
        pytest.skip(f"docker compose up failed: {started.stderr.strip()[-300:]}")
    try:
        if not _wait_ready(DEFAULT_URL, DEFAULT_API_KEY):
            pytest.skip("the integration Lidarr did not become ready within 180s")
        yield DEFAULT_URL, DEFAULT_API_KEY
    finally:
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v"], capture_output=True, timeout=120)


def _upsert_profile(admin: LidarrAdmin, name: str, allowed_types: set[str]) -> int:
    existing = next((p for p in admin.get("metadataprofile") if p["name"] == name), None)
    base = admin.get("metadataprofile/schema")
    for item in base["primaryAlbumTypes"]:
        item["allowed"] = item["albumType"]["name"] in allowed_types
    # Studio albums only, official releases only: keeps the release lists identical to the stock "Standard".
    for item in base["secondaryAlbumTypes"]:
        item["allowed"] = item["albumType"]["name"] == "Studio"
    for item in base["releaseStatuses"]:
        item["allowed"] = item["releaseStatus"]["name"] == "Official"
    body = {**base, "name": name}
    if existing:
        body["id"] = existing["id"]
        return admin.put(f"metadataprofile/{existing['id']}", body)["id"]
    return admin.post("metadataprofile", body)["id"]


def _upsert_root(admin: LidarrAdmin, path: str, body: dict[str, Any]) -> dict[str, Any]:
    existing = next((r for r in admin.get("rootfolder") if r["path"].rstrip("/") == path), None)
    payload = {"name": path.strip("/"), "path": path, **body}
    if existing:
        return admin.put(f"rootfolder/{existing['id']}", {**payload, "id": existing["id"]})
    return admin.post("rootfolder", payload)


@pytest.fixture(scope="session")
def lidarr_admin(lidarr_target) -> LidarrAdmin:
    return LidarrAdmin(*lidarr_target)


@pytest.fixture(scope="session")
def lidarr_setup(lidarr_admin) -> dict[str, Any]:
    """Creates the profiles, tag and root folders the tests assert on; returns their ids."""
    others = [a for a in lidarr_admin.artists() if a.get("foreignArtistId") != ARTIST_MBID]
    if others:
        pytest.skip("this Lidarr already holds artists besides the test artist; use a throwaway instance")
    quality = next((q for q in lidarr_admin.get("qualityprofile") if q["name"] == QUALITY_NAME), None)
    if quality is None:
        pytest.skip(f"Lidarr has no {QUALITY_NAME!r} quality profile")
    with_singles = _upsert_profile(lidarr_admin, PROFILE_WITH_SINGLES, {"Album", "EP", "Single"})
    no_singles = _upsert_profile(lidarr_admin, PROFILE_NO_SINGLES, {"Album", "EP"})
    tag = next((t for t in lidarr_admin.get("tag") if t["label"] == TAG_LABEL), None) or lidarr_admin.post(
        "tag", {"label": TAG_LABEL}
    )
    main = _upsert_root(
        lidarr_admin,
        ROOT_MAIN,
        {
            "defaultQualityProfileId": quality["id"],
            "defaultMetadataProfileId": with_singles,
            "defaultMonitorOption": MONITOR_OPTION,
            "defaultNewItemMonitorOption": NEW_ITEM_OPTION,
            "defaultTags": [tag["id"]],
        },
    )
    nosingles_root = _upsert_root(
        lidarr_admin,
        ROOT_NO_SINGLES,
        {
            "defaultQualityProfileId": quality["id"],
            "defaultMetadataProfileId": no_singles,
            "defaultMonitorOption": "none",
            "defaultNewItemMonitorOption": "none",
            "defaultTags": [],
        },
    )
    yield {
        "quality_id": quality["id"],
        "with_singles_id": with_singles,
        "no_singles_id": no_singles,
        "tag_id": tag["id"],
        "root": main,
        "root_no_singles": nosingles_root,
    }
    lidarr_admin.delete_test_artist()


@pytest.fixture(autouse=True)
def _fresh_caches():
    invalidate_add_defaults()
    yield
    invalidate_add_defaults()


@pytest.fixture(scope="session")
def metadata_network(lidarr_admin, lidarr_setup) -> dict[str, Any]:
    """Skips when Lidarr cannot reach its metadata server, or when it no longer returns the expected artist first."""
    try:
        found = lidarr_admin.get("artist/lookup?term=" + ARTIST_NAME.replace(" ", "%20"))
    except httpx.HTTPError as exc:
        pytest.skip(f"Lidarr artist lookup failed (is api.lidarr.audio reachable?): {exc}")
    if not found:
        pytest.skip("Lidarr artist lookup returned nothing (no internet for api.lidarr.audio / MusicBrainz?)")
    if found[0].get("foreignArtistId") != ARTIST_MBID:
        pytest.skip(f"lookup for {ARTIST_NAME!r} no longer ranks MBID {ARTIST_MBID} first")
    return found[0]


def make_client(base_url: str, api_key: str, root_folder: str = ROOT_MAIN, **kwargs: Any) -> LidarrClient:
    kwargs.setdefault("auto_search", False)
    return LidarrClient(base_url, api_key, root_folder=root_folder, timeout=60.0, **kwargs)


@pytest.fixture
def client(lidarr_target, lidarr_setup) -> LidarrClient:
    return make_client(*lidarr_target)


@pytest.fixture
def clean_artist(lidarr_admin, lidarr_setup, metadata_network):
    """Guarantees the test artist is not in Lidarr when the test starts, and removes it afterwards."""
    lidarr_admin.delete_test_artist()
    yield
    lidarr_admin.delete_test_artist()


@pytest.fixture
def settled_artist(lidarr_admin, lidarr_setup, metadata_network, client) -> dict[str, Any]:
    """The test artist under the singles-allowed profile, fully loaded, nothing monitored."""
    artist = lidarr_admin.test_artist()
    if artist is None or artist["metadataProfileId"] != lidarr_setup["with_singles_id"]:
        lidarr_admin.delete_test_artist()
        added = client.add_artist_with_defaults(metadata_network, whole_artist=False)
        albums, settled = client.wait_for_artist_albums(added["id"], attempts=120, seconds=1.0)
        assert settled, "the test artist never finished loading in Lidarr"
        artist = lidarr_admin.test_artist()
    lidarr_admin.wait_add_options_consumed(artist["id"])
    lidarr_admin.unmonitor_all(artist["id"])
    return artist


class RecordingClient(httpx.Client):
    """httpx.Client that records ``(METHOD, path-after-/api/v1/, json-or-None)`` for each request it sends."""

    calls: list[tuple[str, str, Any]] = []

    def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        import json as _json

        path = request.url.raw_path.decode().split("/api/v1/", 1)[-1]
        body = None
        if request.content:
            try:
                body = _json.loads(request.content)
            except ValueError:
                body = request.content
        type(self).calls.append((request.method, path, body))
        return super().send(request, **kwargs)


@pytest.fixture
def recorded_calls(monkeypatch) -> list[tuple[str, str, Any]]:
    """Records every HTTP call LidarrClient makes (real transport, real Lidarr)."""
    calls: list[tuple[str, str, Any]] = []

    class _Recorder(RecordingClient):
        pass

    _Recorder.calls = calls
    monkeypatch.setattr(lidarr_mod.httpx, "Client", _Recorder)
    return calls


# --------------------------------------------------------------------------- Navidrome (Subsonic API)

NAVIDROME_DEFAULT_URL = "http://127.0.0.1:14533"
# TEST-ONLY credentials for the throwaway container. Not secrets.
NAVIDROME_USER = "it-admin"
NAVIDROME_PASSWORD = "it-admin-password-1"


def _navidrome_up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/ping", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def _navidrome_ensure_admin(url: str, user: str, password: str) -> bool:
    """First start has no users: ``POST /auth/createAdmin`` makes the first admin. Later starts already have one;
    either way the credentials must then log in."""
    httpx.post(f"{url}/auth/createAdmin", json={"username": user, "password": password}, timeout=15.0)
    login = httpx.post(f"{url}/auth/login", json={"username": user, "password": password}, timeout=15.0)
    return login.status_code == 200


@pytest.fixture(scope="session")
def navidrome_target():
    """``(url, user, password)`` of a ready Navidrome holding the generated library; starts the compose stack when
    none is running (and then tears it down). NAVIDROME_IT_URL / _USER / _PASSWORD select an existing throwaway one."""
    from tests.integration.navidrome.make_music import generate

    url = os.environ.get("NAVIDROME_IT_URL")
    if url:
        user = os.environ.get("NAVIDROME_IT_USER", NAVIDROME_USER)
        password = os.environ.get("NAVIDROME_IT_PASSWORD", NAVIDROME_PASSWORD)
        if not _navidrome_up(url.rstrip("/")):
            pytest.skip(f"Navidrome at {url} did not answer /ping")
        yield url.rstrip("/"), user, password
        return
    generate()
    started_here = False
    if not _navidrome_up(NAVIDROME_DEFAULT_URL):
        if shutil.which("docker") is None:
            pytest.skip("no Navidrome reachable and no docker CLI: run `docker compose -f docker-compose.integration.yml up -d navidrome`")
        started = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "navidrome"], capture_output=True, text=True, timeout=600
        )
        if started.returncode != 0:
            pytest.skip(f"docker compose up failed: {started.stderr.strip()[-300:]}")
        started_here = True
    try:
        deadline = time.monotonic() + 120.0
        while not _navidrome_up(NAVIDROME_DEFAULT_URL):
            if time.monotonic() > deadline:
                pytest.skip("the integration Navidrome did not become ready within 120s")
            time.sleep(1.0)
        if not _navidrome_ensure_admin(NAVIDROME_DEFAULT_URL, NAVIDROME_USER, NAVIDROME_PASSWORD):
            pytest.skip("could not create or log in the integration Navidrome admin (is it a fresh throwaway instance?)")
        yield NAVIDROME_DEFAULT_URL, NAVIDROME_USER, NAVIDROME_PASSWORD
    finally:
        if started_here:
            subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v"], capture_output=True, timeout=120)


# --------------------------------------------------------------------------- Jellyfin

JELLYFIN_DEFAULT_URL = "http://127.0.0.1:18096"
# TEST-ONLY credentials for the throwaway container. Not secrets.
JELLYFIN_ADMIN = "it-admin"
JELLYFIN_ADMIN_PASSWORD = "it-admin-password-1"
JELLYFIN_SECOND_USER = "it-kid"
JELLYFIN_SECOND_PASSWORD = "it-kid-password-1"
JELLYFIN_MUSIC_PATH = "/media/music"
_JF_CLIENT = 'Client="trackseerr-it", Device="it", DeviceId="trackseerr-it", Version="1"'


@dataclass(frozen=True)
class JellyfinTarget:
    url: str
    api_key: str
    admin_name: str
    admin_id: str
    kid_name: str
    kid_id: str


def _jellyfin_public(url: str) -> Optional[dict[str, Any]]:
    try:
        resp = httpx.get(f"{url}/System/Info/Public", timeout=3.0)
    except httpx.HTTPError:
        return None
    return resp.json() if resp.status_code == 200 else None


def _jellyfin_complete_wizard(url: str) -> None:
    """Runs the first-run Startup wizard (no-op once ``StartupWizardCompleted``)."""
    info = _jellyfin_public(url)
    if info is None or info.get("StartupWizardCompleted"):
        return
    http = httpx.Client(base_url=url, timeout=60.0)
    http.post("/Startup/Configuration", json={"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"}).raise_for_status()
    http.get("/Startup/User")  # the first call creates the default user the next one renames
    http.post("/Startup/User", json={"Name": JELLYFIN_ADMIN, "Password": JELLYFIN_ADMIN_PASSWORD}).raise_for_status()
    http.post("/Startup/RemoteAccess", json={"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False}).raise_for_status()
    http.post("/Startup/Complete").raise_for_status()


def _jellyfin_setup(url: str) -> Optional[JellyfinTarget]:
    """Wizard, admin login, music library over the generated folder, second user and an API key. Idempotent."""
    _jellyfin_complete_wizard(url)
    http = httpx.Client(base_url=url, timeout=60.0)

    def login(name: str, password: str) -> Optional[dict[str, Any]]:
        resp = http.post(
            "/Users/AuthenticateByName",
            headers={"Authorization": f"MediaBrowser {_JF_CLIENT}"},
            json={"Username": name, "Pw": password},
        )
        return resp.json() if resp.status_code == 200 else None

    session = login(JELLYFIN_ADMIN, JELLYFIN_ADMIN_PASSWORD)
    if session is None:
        return None
    auth = {"Authorization": f'MediaBrowser {_JF_CLIENT}, Token="{session["AccessToken"]}"'}
    folders = http.get("/Library/VirtualFolders", headers=auth).json()
    if not any(f.get("Name") == "Music" for f in folders):
        http.post(
            "/Library/VirtualFolders",
            params={"name": "Music", "collectionType": "music", "refreshLibrary": "true"},
            headers=auth,
            json={"LibraryOptions": {"EnableInternetProviders": False, "PathInfos": [{"Path": JELLYFIN_MUSIC_PATH}]}},
        ).raise_for_status()
    users = {u["Name"]: u["Id"] for u in http.get("/Users", headers=auth).json()}
    if JELLYFIN_SECOND_USER not in users:
        created = http.post("/Users/New", headers=auth, json={"Name": JELLYFIN_SECOND_USER, "Password": JELLYFIN_SECOND_PASSWORD})
        created.raise_for_status()
        users[JELLYFIN_SECOND_USER] = created.json()["Id"]
    keys = http.get("/Auth/Keys", headers=auth).json().get("Items", [])
    key = next((k["AccessToken"] for k in keys if k.get("AppName") == "trackseerr-it"), None)
    if key is None:
        http.post("/Auth/Keys", params={"app": "trackseerr-it"}, headers=auth).raise_for_status()
        keys = http.get("/Auth/Keys", headers=auth).json().get("Items", [])
        key = next(k["AccessToken"] for k in keys if k.get("AppName") == "trackseerr-it")
    return JellyfinTarget(url, key, JELLYFIN_ADMIN, users[JELLYFIN_ADMIN], JELLYFIN_SECOND_USER, users[JELLYFIN_SECOND_USER])


@pytest.fixture(scope="session")
def jellyfin_target():
    """A ready Jellyfin with the generated library, an admin, a second user and an API key; starts the compose
    stack's ``jellyfin`` service when none is running (and then tears the stack down with ``down -v``).
    JELLYFIN_IT_URL selects an existing throwaway instance that was already set up by this fixture."""
    from tests.integration.navidrome.make_music import generate

    url = os.environ.get("JELLYFIN_IT_URL")
    if url:
        target = _jellyfin_setup(url.rstrip("/"))
        if target is None:
            pytest.skip(f"could not log in to Jellyfin at {url} as {JELLYFIN_ADMIN} (is it a fresh throwaway instance?)")
        yield target
        return
    generate()
    started_here = False
    if _jellyfin_public(JELLYFIN_DEFAULT_URL) is None:
        if shutil.which("docker") is None:
            pytest.skip("no Jellyfin reachable and no docker CLI: run `docker compose -f docker-compose.integration.yml up -d jellyfin`")
        started = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "jellyfin"], capture_output=True, text=True, timeout=900
        )
        if started.returncode != 0:
            pytest.skip(f"docker compose up failed: {started.stderr.strip()[-300:]}")
        started_here = True
    try:
        deadline = time.monotonic() + 180.0
        while _jellyfin_public(JELLYFIN_DEFAULT_URL) is None:
            if time.monotonic() > deadline:
                pytest.skip("the integration Jellyfin did not become ready within 180s")
            time.sleep(2.0)
        target = _jellyfin_setup(JELLYFIN_DEFAULT_URL)
        if target is None:
            pytest.skip("could not set up the integration Jellyfin (is it a fresh throwaway instance?)")
        yield target
    finally:
        if started_here:
            subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v"], capture_output=True, timeout=120)
