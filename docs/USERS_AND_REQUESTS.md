# Users and requests

- [Signing in](#signing-in)
- [Adding users](#adding-users)
- [Permissions](#permissions)
- [Quotas](#quotas)
- [Making a request](#making-a-request)
- [Approving requests](#approving-requests)
- [Issues](#issues)
- [Account security](#account-security)

## Signing in

There are two kinds of accounts.

| Account | How it signs in | Who creates it |
|---|---|---|
| Plex | **Sign in with Plex** | Created the first time a Plex user signs in. Only users with access to your Plex server are let in. |
| Local | Username and password | An admin creates it and sends an invite link. There is no public sign-up. |

The first admin is either the Plex server owner or the local admin created from `ADMIN_PASSWORD`. See [Install guide: first sign-in](INSTALL.md#first-sign-in).

If your media server is Jellyfin, its users are imported as playlist targets, but they sign in to TrackSeerr with local accounts.

In the [two-container setup](INSTALL.md#two-containers), users sign in on the gateway and admins sign in on the core. Admin pages are not available on the gateway.

## Adding users

Open **Settings > Requests > Users**.

To add a local user:

1. Click **Create local user**.
2. Enter a username. Email is optional.
3. Copy the invite link and send it to the user. The link works once and expires after 48 hours.
4. The user opens the link and sets a password of at least 12 characters.

Plex users appear on this page after their first sign-in. Jellyfin and Plex Home users also appear here when TrackSeerr imports them from the media server.

From the user list you can:

- change permissions and quotas
- send a new password reset link
- reset two-factor authentication
- sign the user out everywhere
- disable or delete the user

You cannot remove your own admin permission or delete the last admin.

## Permissions

| Permission | What it allows |
|---|---|
| Admin | Everything. Only granted on the core or a single container. |
| Request music | Submit requests. |
| Auto-approve tracks | Track requests skip approval. |
| Auto-approve albums | Album requests skip approval. |
| Auto-approve discographies | Discography requests skip approval. |
| Report issues | Report problems with music in the library. |
| Auto-request playlist tracks | Missing tracks from this user's playlists are requested on their behalf. |
| Manage requests | See every user's requests and approve or reject them. Works on the core or a single container only, never through the gateway. |

New users get **Request music** and **Report issues**. Set different defaults in **Settings > Requests > Users > Account defaults**.

To approve every request from every user, set `AUTO_APPROVE_REQUESTS=1`.

## Quotas

A quota limits how many requests a user can make in a rolling window. There is one limit per request type.

| Setting | Default |
|---|---|
| Tracks per window | 25 |
| Albums per window | 10 |
| Discographies per window | 1 |
| Window | 7 days |

Set the defaults in **Settings > Requests > Users**. Override them for one user in that user's settings. Rejected and cancelled requests do not count. Admins have no quota.

Users see what they have left in **Settings > Account**.

## Making a request

1. Open **Discover** and search for an artist, album, or track.
2. Open the album or artist page. Play previews if you want.
3. Click request on a track, an album, or the whole discography.

Follow requests in **Requests**. Each request moves through these states:

| State | Meaning |
|---|---|
| Pending | Waiting for an admin. |
| Approved | Approved and waiting to be searched for. |
| Processing | Found and downloading. |
| Available | In the library and on the media server. |
| Rejected | Turned down by an admin. |

## Approving requests

Admins, and users with **Manage requests**, see pending requests in **Requests > Pending**. Approve or reject each one. In the two-container setup this only works on the core. When you approve a request, TrackSeerr sends it to the library manager: its own downloader, or Lidarr.

Set up notifications in **Settings > Requests > Notifications** to hear about new requests. Supported channels are Discord, Telegram, Pushover, email, and webhooks.

## Issues

Users with **Report issues** can flag a problem with an album or track from its page in **Discover**. Issue types:

- audio quality
- corrupted file
- wrong release
- missing tracks
- incorrect tags
- request stuck
- other

Users follow their reports in **Requests > My issues**. Admins handle them in **Activity > Issues**. From an issue an admin can comment, resolve it, or blocklist the release and search for a new one. Users get a notice when their issue has new activity.

## Account security

Each user manages their own account in **Settings > Account**.

- **Password.** Local accounts only. 12 to 128 characters. It cannot contain the username or be a common password.
- **Two-factor authentication.** Local accounts can turn on codes from an authenticator app. Setup also gives 10 single-use recovery codes. An admin can require two-factor authentication for all local accounts in **Settings > Requests > Users**.
- **Lockout.** Five failed sign-ins in 15 minutes lock the account for 15 minutes. Twenty failures from one IP in 15 minutes block that IP for a while.

Plex accounts use Plex's own sign-in and security.
