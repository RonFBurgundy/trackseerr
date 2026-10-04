# Lidarr integration (contract) tests

`tests/integration/` runs Trackseerr's Lidarr client against a real Lidarr container. `tests/lidarr_fake.py` (used by the
unit tests) was written from Lidarr's source; these tests are the check that the real API still matches it. They are
**not collected** unless `RUN_INTEGRATION=1`, so the normal suite is unaffected.

## Run

```bash
RUN_INTEGRATION=1 docker compose -f docker-compose.integration.yml up -d     # Lidarr on 127.0.0.1:18686, ~20s to boot
# Host has the dev deps:
RUN_INTEGRATION=1 pytest tests/integration -m integration -p no:xdist
# or inside the test image (host networking reaches the loopback-bound port):
docker run --rm --network host -e RUN_INTEGRATION=1 -v "$PWD":/app -w /app -e PYTHONPATH=/app \
  trackseerr:test pytest tests/integration -q --tb=short
docker compose -f docker-compose.integration.yml down -v
```

Without a running stack, and with the `docker` CLI available, the session fixture starts the stack itself and tears it
down afterwards (a stack that was already running is left alone). To use another instance set `LIDARR_IT_URL` and
`LIDARR_IT_API_KEY`; it must be throwaway (the fixtures create root folders `/music` and `/music-nosingles`, the
`IT-WithSingles` / `IT-NoSingles` metadata profiles and the `it-tag` tag, and delete the test artist; the session is
skipped if the instance holds other artists).

Needs internet: Lidarr fetches metadata from api.lidarr.audio / MusicBrainz. Tests skip with a reason if the lookup
fails. About 10 s, three artist adds in total.

## The stack

`docker-compose.integration.yml`: `lscr.io/linuxserver/lidarr:3.1.0.4875-ls42` (Lidarr 3.1.0.4875, the stable
release at the time of writing; API v1), `PUID/PGID` 1000, loopback-only port, `/config` and both root folders on
tmpfs. No download clients, no indexers. `tests/integration/lidarr/config.xml` is copied into `/config` by a
linuxserver `custom-cont-init.d` hook and carries a fixed **test-only** `ApiKey` (not a secret) and
`AuthenticationMethod=None`.

Test artist: **Neutral Milk Hotel** (MBID a506f761-2c22-4b2f-8a94-bd748c2c8f75), a finished band so the MusicBrainz data
does not grow. Under a singles-allowed profile Lidarr lists 7 releases: 2 albums, 1 EP, 4 singles. "Holland, 1945" is on
both the album *In the Aeroplane Over the Sea* and its own single; "Unborn" exists only on the single *Everything Is*.

## Covered (`tests/integration/test_lidarr_contract.py`)

1. Root-folder defaults: `source == "rootfolder"` and exactly the configured quality/metadata profile, monitor options and tag; field names confirmed on the raw resource; a second root folder selected by path.
2. `metadata_profile_allows_singles`: true / false for the with/without-Singles profiles.
3. Whole-artist add: profiles, tag, root folder, `monitorNewItems`, `addOptions.monitor` from the root folder; `future` leaves nothing monitored.
4. Song request for a new artist: `monitor: none` add, `artist_refresh_state` observed `running` then `done`, exactly one album monitored (the Single), Lidarr's re-read confirms, `AlbumSearch` command shape accepted.
5. Release selection on real album/track lists: `prefer_singles` True picks the Single, False the album. `GET /track?artistId=` works (no per-album fallback was needed).
6. Request against an existing artist: no POST/PUT/DELETE on the artist, artist unchanged (unmonitored stays unmonitored).
7. `not_in_metadata_profile`: no-singles profile, song only on a single, nothing monitored.
8. `GET /api/settings/lidarr/defaults` through the FastAPI TestClient against the real Lidarr, same shape as `tests/test_lidarr_follow_settings.py::TestDefaultsEndpoint::test_shape`.

## Findings against real Lidarr 3.1.0.4875

Confirmed as the client and the fake assume: rootfolder `default*` field names; `primaryAlbumTypes[].albumType.name` /
`allowed`; `GET /command` rows `{name: "RefreshArtist", status: lowercase, body.artistIds: [id]}` with statuses
`started` then `completed`; `POST /artist` 201 with the artist; `PUT /album/monitor` 202; `POST /command` 201;
`/track?artistId=` and `/track?albumId=` both work; `GET /track` without a filter is 400; unknown metadata profile 404.

1. **Race in the song-request flow (fixed).** For a new artist added with
   `addOptions.monitor = "none"`, `GET /album?artistId=` lists *every* album as `monitored: true` while RefreshArtist
   runs, and Lidarr only applies `monitor: none` after the command already reads `completed` (then `artist.addOptions`
   becomes `null`). In about 1 of 10 runs the client polls inside that window: `wait_for_artist_albums` returns
   settled, `_monitor_wants` sees the chosen album as already monitored, skips the `PUT /album/monitor`, and the
   verification re-read finds nothing monitored, so the request ends `monitor_failed` with nothing monitored. Observed
   timeline: running 0.2-1.3 s, `done` with 7/7 monitored at 1.34 s, 7/0 at 1.47 s. Fix: a just-added artist counts as
   settled only once `GET /artist/{id}` has `addOptions == null`, and the client always sends `PUT /album/monitor` for
   an artist it added in the same call. The fake models this window behind `model_add_window`
   (`tests/test_lidarr_add_options_race.py`); test 4 passed 16/16 against real Lidarr after the fix.
2. Stock "Standard" metadata profile (id 1) does **not** allow Singles (only Album). The fake's "Standard" does; naming
   only. Lidarr's album list for an artist already honours the profile (no Singles listed under a no-singles profile).
3. A lookup row for an artist not yet in Lidarr has no `id` key; the fake used `"id": 0`. The client handles both, so
   the fake now omits it (`tests/lidarr_fake.py`).
4. `GET /command` also lists `RescanFolders` rows whose `body.artistIds` carries the artist id; the client filters by
   name, so no effect.
5. A metadata profile that allows nothing (stock "None") makes a new artist list zero albums forever;
   `wait_for_artist_albums` then returns `([], False)` and requests end `albums_pending`.
