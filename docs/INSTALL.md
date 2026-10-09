# Install guide

This guide covers installing TrackSeerr with Docker Compose. For Unraid, see the [Unraid guide](UNRAID_INSTALL_GUIDE.md).

- [Choose a setup](#choose-a-setup)
- [Folders](#folders)
- [Single container](#single-container)
- [Two containers](#two-containers)
- [First sign-in](#first-sign-in)
- [Reverse proxies and the application URL](#reverse-proxies-and-the-application-url)
- [Move from one container to two](#move-from-one-container-to-two)
- [Generate the two-container files with init-dmz](#generate-the-two-container-files-with-init-dmz)
- [Updating](#updating)

## Choose a setup

TrackSeerr runs from one image. The `ROLE` variable decides what each container does.

| Setup | `ROLE` | Use it when |
|---|---|---|
| Single container | `all-in-one` (default) | TrackSeerr is only reachable on your LAN, a VPN, or Tailscale |
| Two containers | `gateway` + `core` | TrackSeerr is reachable from the internet through a reverse proxy, tunnel, or port forward |

A single container holds everything: the web app, your media server token, download client passwords, and write access to your music. If someone breaks into the web app, they reach all of that.

In the two-container setup, the public container (the gateway) holds no files and no secrets except a signing key. It can only do what a normal user can do. The core holds everything else and is only reachable from your LAN. See [Architecture and security](ARCHITECTURE_AND_SECURITY.md#two-container-setup) for details.

## Folders

TrackSeerr uses two mounts.

| Container path | What goes there |
|---|---|
| `/config` | Database, settings, logs, session keys. Back this up. |
| `/data` | Your music and your downloads. |

Put your music and your downloads under the same `/data` mount, for example:

```
/data
├── downloads      # download clients save here
└── media
    └── music      # your library
```

With both on one mount, TrackSeerr can move files instantly and can hardlink torrents so they keep seeding without using extra space. This is the layout recommended by [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/).

Your download clients must see the same paths. If qBittorrent saves to `/data/downloads/music`, TrackSeerr must also see that folder as `/data/downloads/music`. TrackSeerr reads each client's download folder from its API and shows any mismatch in **Settings > Media Management > Root Folders & Naming**.

`PUID` and `PGID` set the user that owns the files TrackSeerr writes. Use the same IDs as your media server and download clients. On Unraid that is `99` and `100`.

## Single container

1. Create a folder for TrackSeerr and save this as `docker-compose.yml`:

   ```yaml
   services:
     trackseerr:
       image: ghcr.io/ronfburgundy/trackseerr:latest
       container_name: trackseerr
       restart: unless-stopped
       ports:
         - "5250:5250"
       volumes:
         - ./appdata:/config
         - /path/to/data:/data
       environment:
         - PUID=1000
         - PGID=1000
         - UMASK=022
         - TZ=Etc/UTC
         # Pick one way to sign in the first time (see "First sign-in"):
         - PLEX_URL=http://192.168.1.100:32400
         - PLEX_TOKEN=your_plex_token
         # - ADMIN_PASSWORD=change-me-to-12-or-more-characters
   ```

2. Change `/path/to/data` to your data folder.
3. Start it:

   ```bash
   docker compose up -d
   ```

4. Open `http://<server-ip>:5250`.

Do not use `localhost` in URLs for other services. Inside a container, `localhost` is the container itself. Use the server's LAN IP or the other container's name.

The repository also has a longer example with every option commented: [`docker-compose.yml`](../docker-compose.yml).

## Two containers

The gateway faces the internet. The core stays on your LAN. They talk over a private Docker network that has no internet access.

```
Internet ──> reverse proxy ──> gateway ──(internal network)──> core ──> media server,
                               (no files,                      download clients,
                                no secrets)                    music folder
```

1. Generate a shared secret and save it in a `.env` file next to your compose file:

   ```bash
   echo "INTERNAL_CORE_SECRET=$(openssl rand -hex 32)" >> .env
   ```

2. Add the LAN IP of the Docker host to `.env`. The core's admin page will listen on this address only:

   ```bash
   echo "CORE_LAN_BIND=192.168.1.10" >> .env
   ```

   The default is `127.0.0.1`, which only works from the host itself. Never expose port 5251 to the internet.

3. Save this as `docker-compose.yml` (the same file is in the repository as [`docker-compose.hardened.yml`](../docker-compose.hardened.yml)):

   ```yaml
   services:
     trackseerr-gateway:
       image: ghcr.io/ronfburgundy/trackseerr:latest
       container_name: trackseerr-gateway
       restart: unless-stopped
       ports:
         - "5250:5250"
       environment:
         - ROLE=gateway
         - INTERNAL_CORE_SECRET=${INTERNAL_CORE_SECRET:?set a 32+ char secret}
         - TRACKSEERR_CORE_URL=http://trackseerr-core:5251
         - APPLICATION_URL=https://music.example.com
         - PUID=1000
         - PGID=1000
         - TZ=Etc/UTC
       networks:
         - proxynet
         - internal

     trackseerr-core:
       image: ghcr.io/ronfburgundy/trackseerr:latest
       container_name: trackseerr-core
       restart: unless-stopped
       ports:
         - "${CORE_LAN_BIND:-127.0.0.1}:5251:5251"
       volumes:
         - ./appdata:/config
         - /path/to/data:/data
       environment:
         - ROLE=core
         - PORT=5251
         - CORE_LAN_BIND=${CORE_LAN_BIND:-127.0.0.1}
         - INTERNAL_CORE_SECRET=${INTERNAL_CORE_SECRET:?set a 32+ char secret}
         - APPLICATION_URL=https://music.example.com
         - PUID=1000
         - PGID=1000
         - TZ=Etc/UTC
         - PLEX_URL=http://192.168.1.100:32400
         - PLEX_TOKEN=your_plex_token
       networks:
         - internal
         - core-lan

   networks:
     proxynet:
       name: ${PROXY_NETWORK:-proxynet}
       external: true
     internal:
       name: ${TRACKSEERR_INTERNAL_NETWORK:-trackseerr-internal}
       internal: true
     core-lan:
       name: trackseerr-core-lan
       driver: bridge
   ```

4. Start it with `docker compose up -d`.

Who uses which address:

- **Users** go to the gateway, through your public URL.
- **Admins** go to the core at `http://<CORE_LAN_BIND>:5251`. Admin pages are not available through the gateway.

Rules for the two containers:

- Put secrets on the core only: media server tokens, download client passwords, Last.fm secrets. The gateway refuses to start if it finds them.
- The gateway joins the internal network and your reverse proxy's network (`proxynet` above, set with `PROXY_NETWORK`).
- The core joins the internal network and a normal network so it can reach your media server and download clients.
- If you use Cloudflare Tunnel, `cloudflared` must share a network with the gateway, never with the core. Point the tunnel at `http://trackseerr-gateway:5250`.
- Set `TRUSTED_PROXIES` on the gateway to your proxy's IP or subnet. Without it, every user appears to come from the proxy's IP, and login rate limits apply to everyone at once.

## First sign-in

TrackSeerr needs one admin account. How you get it depends on your media server.

- **Plex:** set `PLEX_URL` and `PLEX_TOKEN`. The Plex server owner signs in with **Sign in with Plex** and becomes the admin.
- **Jellyfin, Subsonic, or no media server:** set `ADMIN_PASSWORD` (at least 12 characters) before the first start. TrackSeerr creates a local admin named `admin` (change it with `ADMIN_USERNAME`). Sign in, then connect your media server in **Settings > Media Management > Media Server**.

`ADMIN_PASSWORD` is only used when no admin exists. You can remove it after the first start.

To find your Plex token, follow Plex's guide: [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

After you sign in, a typical setup order is:

1. **Settings > Media Management > Media Server**: connect your media server (if not set by environment variables).
2. **Settings > General > Library manager**: choose TrackSeerr or Lidarr.
3. **Settings > Media Management > Root Folders & Naming**: set the music folder, for example `/data/media/music`.
4. **Settings > Media Management > Clients** and **Indexers**: add download clients and indexers. See [Downloading music](ACQUISITION.md).
5. **Library**: run a scan so TrackSeerr knows what you already have.
6. **Settings > Requests > Users**: set quotas and add users. See [Users and requests](USERS_AND_REQUESTS.md).

## Reverse proxies and the application URL

If users reach TrackSeerr through a domain name, set the application URL. Use the `APPLICATION_URL` variable or **Settings > General > Application URL**. Example: `https://music.example.com`.

TrackSeerr uses it for:

- links in notifications
- the return address after Plex sign-in
- invite and password reset links for local accounts

Any reverse proxy works (SWAG, Nginx Proxy Manager, Traefik, Caddy). Proxy all paths to port 5250. If you expose TrackSeerr to the internet, use the [two-container setup](#two-containers).

## Move from one container to two

Your existing container becomes the core. The image, `/config`, and database stay the same. Nothing is exported or imported.

1. Generate a secret: `openssl rand -hex 32`.
2. Edit the existing container:
   - set `ROLE=core`
   - set `INTERNAL_CORE_SECRET` to the secret
   - set `PORT=5251` and publish port 5251 on your LAN IP
   - set `APPLICATION_URL` to the public URL users will use
   - add it to the internal network
3. Start it. The core logs a one-time checklist and shows it as a banner in the admin UI.
4. Create the gateway container with the same secret and `APPLICATION_URL`, plus `TRACKSEERR_CORE_URL=http://<core container name>:5251`. Put it on the internal network and the proxy network.
5. Point your reverse proxy or tunnel at the gateway.
6. Users sign in again on the gateway. The Plex webhook URL does not change.

To go back, set `ROLE=all-in-one` on the core and remove the gateway.

## Generate the two-container files with init-dmz

`init-dmz` writes a `docker-compose.dmz.yml` and a `.env` for the two-container setup, and prints the values for the Unraid templates. It never overwrites a file; if one exists, it writes `<name>.new`. It makes no network calls.

To build the files from your current single container, run it inside that container:

```bash
docker exec -it trackseerr python -m trackseerr init-dmz \
  --from-existing --public-url https://music.example.com --out /config/dmz
```

The files land in `appdata/dmz` on the host.

| Option | Meaning |
|---|---|
| `--from-existing` | Copy non-secret settings from the current environment into the core service. Secrets are written to `.env` only. |
| `--env-file PATH` | Read existing settings from this file instead. |
| `--public-url URL` | The public URL (`APPLICATION_URL`). |
| `--core-lan-bind IP` | LAN IP for the core's admin port. Default `127.0.0.1`. `0.0.0.0` is refused. |
| `--network NAME` | Internal network name. Default `trackseerr-internal`. |
| `--out DIR` | Output folder. Default is the current folder. |

The `.env` file is created with mode `0600`.

## Updating

```bash
docker compose pull
docker compose up -d
```

Database changes run automatically on start. Back up `/config` before updating.
