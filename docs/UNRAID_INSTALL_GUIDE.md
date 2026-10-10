# Unraid guide

TrackSeerr ships three Unraid templates.

| Template | Container name | Use it for |
|---|---|---|
| `trackseerr.xml` | `TrackSeerr` | Single container. LAN, VPN, or Tailscale only. |
| `trackseerr-core.xml` | `TrackSeerr-Core` | Two containers: the private core. |
| `trackseerr-requests.xml` | `TrackSeerr-Requests` | Two containers: the public gateway. |

If TrackSeerr will be reachable from the internet, use the two-container setup. See [Install guide: choose a setup](INSTALL.md#choose-a-setup) for why.

- [Folders](#folders)
- [Single container](#single-container)
- [Two containers](#two-containers)
- [Without Plex](#without-plex)
- [Troubleshooting](#troubleshooting)

## Folders

Use one share for music and downloads, as [TRaSH Guides](https://trash-guides.info/File-and-Folder-Structure/) recommends:

| Host path | Container path | Holds |
|---|---|---|
| `/mnt/user/appdata/trackseerr` | `/config` | Database, settings, logs |
| `/mnt/user/data` | `/data` | `media/music` and `downloads` |

Map `/mnt/user/data` to `/data` in your download clients too, so every container sees the same paths.

Keep `PUID=99` and `PGID=100`, the Unraid defaults.

## Single container

1. Open the Unraid terminal and download the template:

   ```bash
   curl -o /boot/config/plugins/dockerMan/templates-user/my-trackseerr.xml \
     https://raw.githubusercontent.com/RonFBurgundy/trackseerr/main/unraid/trackseerr.xml
   ```

2. Go to **Docker > Add Container** and pick **my-trackseerr** from the **Template** list.
3. Fill in the fields:

   | Field | Value |
   |---|---|
   | AppData Storage | `/mnt/user/appdata/trackseerr` |
   | Data Storage | `/mnt/user/data` |
   | Plex Server URL | Your Plex LAN address, for example `http://192.168.1.100:32400`. Not `localhost`. Leave empty without Plex. |
   | Plex Token | Your Plex token. See [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/). Leave empty without Plex. |
   | Admin Password | Only without Plex. See [Without Plex](#without-plex). |
   | Application URL | Your public URL, if you use one. Otherwise leave it empty. |

4. Click **Apply**.
5. Open the web UI from the container's icon, or go to `http://<unraid-ip>:5250`.
6. Click **Sign in with Plex**, or sign in as `admin` if you set an admin password.

Next, follow the setup order in [Install guide: first sign-in](INSTALL.md#first-sign-in).

## Two containers

1. Create the networks once, in the Unraid terminal:

   ```bash
   docker network create --internal trackseerr-internal
   docker network create trackseerr-core-lan
   ```

   `trackseerr-internal` links the two containers and has no internet access. `trackseerr-core-lan` is a normal bridge that lets the core reach Plex, your downloaders and the internet.

2. Generate a secret and copy it:

   ```bash
   openssl rand -hex 32
   ```

3. Download both templates:

   ```bash
   cd /boot/config/plugins/dockerMan/templates-user
   curl -o my-trackseerr-core.xml \
     https://raw.githubusercontent.com/RonFBurgundy/trackseerr/main/unraid/trackseerr-core.xml
   curl -o my-trackseerr-requests.xml \
     https://raw.githubusercontent.com/RonFBurgundy/trackseerr/main/unraid/trackseerr-requests.xml
   ```

4. Add **TrackSeerr-Core** from its template:
   - **Internal Core Secret**: the secret from step 2.
   - **LAN Bind IP**: your Unraid server's LAN IP.
   - **Application URL**: the public URL users will use.
   - Plex fields as in the single container. Use LAN IPs for Plex and your downloaders.
   - Leave **Network Type** on **Bridge**, so Unraid still publishes the admin port.
   - In **Extra Parameters**, add `--network=trackseerr-core-lan --network=trackseerr-internal`.
   - Never put the core on your reverse-proxy or Cloudflare Tunnel network.

5. Add **TrackSeerr-Requests** from its template:
   - **Internal Core Secret**: the same secret.
   - **Core URL**: leave it as `http://TrackSeerr-Core:5251`. If you renamed the core container, use the new name.
   - **Application URL**: the same public URL.
   - **Trusted Proxies**: the IP or subnet of your reverse proxy.
   - Set **Network Type** to your reverse-proxy network, for example `proxynet`.
   - In **Extra Parameters**, add `--network=proxynet --network=trackseerr-internal`, replacing `proxynet` with your reverse-proxy network.

6. Point your reverse proxy or Cloudflare Tunnel at `TrackSeerr-Requests` on port 5250. Never at the core.

Admins use the core at `http://<unraid-ip>:5251`. Everyone else uses the public URL.

Both networks go in **Extra Parameters** because Unraid ignores the **Network Type** dropdown once Extra Parameters contain a `--network` flag. With only `--network=trackseerr-internal`, the container ends up on the internal network alone: Requests can't see your tunnel or proxy, and the core can't reach Plex.

Multiple `--network` flags need Unraid 7 (Docker 25 or later). On older versions, leave Extra Parameters empty, pick the network in the dropdown (your proxy network for Requests; for the core, Bridge or `trackseerr-core-lan`), start the container, then run:

```bash
docker network connect trackseerr-internal TrackSeerr-Core
docker network connect trackseerr-internal TrackSeerr-Requests
```

Run these again after each container update.

To turn an existing single container into the core, see [Install guide: move from one container to two](INSTALL.md#move-from-one-container-to-two).

## Without Plex

Plex is optional. The Plex fields start empty.

1. Leave **Plex Server URL** and **Plex Token** empty. Fill in both or neither; with only one set, TrackSeerr will not start.
2. Set **Admin Password** to at least 12 characters.
3. Apply, then sign in as `admin` with that password.
4. Connect Jellyfin or a Subsonic server in **Settings > Media Management > Media Server**.

You can also set the media server in the template instead. Set **Media Server** to `jellyfin` or `subsonic` and fill in its fields under **Show more settings**.

See [Media servers](MEDIA_SERVERS.md).

## Troubleshooting

**The web UI does not load, or the log shows database permission errors.** The appdata folder may be owned by root. Fix it in the Unraid terminal, then restart the container:

```bash
chown -R 99:100 /mnt/user/appdata/trackseerr
```

Or set `FORCE_CHOWN=1` for one start.

**TrackSeerr cannot reach Plex or a download client.** Use the server's LAN IP or the container name, not `localhost`. Inside a container, `localhost` is the container itself.

**Downloads finish but are not imported.** TrackSeerr and the download client must see the download folder at the same path. Check **Settings > Media Management > Root Folders & Naming > Download folders**.

**Updating.** Click **Check for Updates** on the **Docker** tab, then **Apply Update**.
