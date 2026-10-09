# Users, Local Accounts, MFA & Request Quotas — design contract

Status: in implementation on branch `feat/users-and-accounts`. This builds on `docs/design/two-tier-security.md`; read that first. Security is the top priority.

Owner decisions (2026-10-03):
- Local (non-Plex) accounts are **admin-created with a one-time invite link**. The user sets their own password, and there is no public signup.
- **Optional TOTP** MFA with recovery codes. The admin can require it for all local accounts.
- **Only admins approve requests**, on the core LAN UI.

## Identities

- Plex users: `users.id` = the numeric plex.tv id, unchanged.
- Local users: `users.id = "local-" + secrets.token_hex(12)`, `auth_type = 'local'`. They are only ever created by an admin on core.
- `ensure_user` (the forwarded-principal path) keeps creating only numeric Plex ids. A `local-*` id must already exist; otherwise it gets 401. It must also refuse tombstoned ids (see below).

## Storage: migration v27 (the highest migration is currently v26)

```sql
ALTER TABLE users ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'plex';          -- plex | local
ALTER TABLE users ADD COLUMN password_hash TEXT;                              -- local only
ALTER TABLE users ADD COLUMN password_changed_at TEXT;
ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN sessions_revoked_at TEXT;                        -- ISO UTC; sessions issued before are invalid
ALTER TABLE users ADD COLUMN last_login_at TEXT;
ALTER TABLE users ADD COLUMN totp_secret TEXT;                                -- base32; NULL = MFA off
ALTER TABLE users ADD COLUMN totp_last_counter INTEGER;                       -- replay protection
ALTER TABLE users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until TEXT;
-- per-type quotas (NULL = use global default); window in days
ALTER TABLE users ADD COLUMN quota_tracks INTEGER;
ALTER TABLE users ADD COLUMN quota_albums INTEGER;
ALTER TABLE users ADD COLUMN quota_discographies INTEGER;
ALTER TABLE users ADD COLUMN quota_window_days INTEGER;
CREATE TABLE IF NOT EXISTS user_invites (
    token_hash TEXT PRIMARY KEY,          -- sha256 hex of the raw token; raw token never stored
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,                -- invite | reset
    expires_at TEXT NOT NULL,             -- 48h
    used_at TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
CREATE TABLE IF NOT EXISTS user_recovery_codes (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,              -- sha256 hex
    used_at TEXT,
    PRIMARY KEY (user_id, code_hash)
);
CREATE TABLE IF NOT EXISTS user_tombstones (
    user_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    deleted_by TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT NOT NULL,                    -- "ip:<addr>" or "user:<lower username>"
    attempted_at INTEGER NOT NULL         -- unix seconds
);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(key, attempted_at);
ALTER TABLE general_settings ADD COLUMN require_mfa_local INTEGER NOT NULL DEFAULT 0;
ALTER TABLE general_settings ADD COLUMN default_quota_tracks INTEGER NOT NULL DEFAULT 25;
ALTER TABLE general_settings ADD COLUMN default_quota_albums INTEGER NOT NULL DEFAULT 10;
ALTER TABLE general_settings ADD COLUMN default_quota_discographies INTEGER NOT NULL DEFAULT 1;
ALTER TABLE general_settings ADD COLUMN default_quota_window_days INTEGER NOT NULL DEFAULT 7;
```

Local usernames must be unique, case-insensitive, across ALL users including Plex users. That prevents impersonating a Plex username. The rule is 3–32 characters from `[a-z0-9._-]`.

## Crypto (stdlib only, no new dependencies)

- **Password hash:** `hashlib.scrypt(n=2**15, r=8, p=1, dklen=32, maxmem=64 MiB)` with a 16-byte random salt.
  - Format: `scrypt$32768$8$1$<salt_b64>$<hash_b64>`.
  - Verify with `hmac.compare_digest`.
  - On a parameter mismatch, re-hash on the next successful login.
  - Unknown usernames still run one dummy scrypt, so response timing doesn't reveal which accounts exist.
- **Password policy:** 12–128 characters. It must not contain the username (case-insensitive). It is rejected if it is in a bundled list of the 1000 most common passwords: add `trackseerr/data/common_passwords.txt` (lowercase, one per line) and ship it in the package.
- **TOTP:** RFC 6238 with SHA-1, 30 s steps, 6 digits, a 20-byte secret, and a window of ±1 step.
  - Reject a counter ≤ `totp_last_counter` (replay).
  - Store the base32 secret. It lives on core only and is never returned after enrollment.
- **Recovery codes:** 10 codes, each `xxxx-xxxx-xxxx` from a 32-symbol alphabet, made with `secrets`. Store them as sha256 hashes. Each is single use and is shown once.
- **Invite and reset tokens:** `secrets.token_urlsafe(32)`, stored as sha256, single use, valid 48 h. Creating a new token for a user voids that user's unused tokens of the same purpose.

## Login, sessions, revocation

- `POST /api/auth/local/login` `{username, password, totp_code?, recovery_code?}`:
  - **On core or all-in-one:** verified directly.
  - **On the gateway:** it is on the gateway LOCAL allowlist. The gateway calls core's `POST /api/internal/auth/local/verify` signed as the **service principal**, with the client IP in the JSON body. Core verifies and returns `{user: {id, username}, mfa_required: bool}` or an error. The gateway then creates its own session exactly as it does for Plex login (same cookie and flags).
  - `/api/internal/*` is never on the gateway allowlist and accepts **only** the signed service principal. Every other caller gets 404.
- **Lockout and throttling,** enforced on core and counted in `login_attempts`:
  - Per username: 5 failures in 15 min locks the account for 15 min.
  - Per IP: 20 failures in 15 min gives 429.
  - Failed TOTP attempts count as failures.
  - Responses are generic: `401 "Invalid username or password"`, `401 {"detail":"mfa_required"}` when the password was right but a code is needed, `423 "Account temporarily locked"`, `429`.
  - The client IP comes from the request's peer address. On the gateway it comes from `X-Forwarded-For` only if `TRUSTED_PROXIES` (a new env var, a CIDR list) contains the peer. Otherwise the peer address is used.
- **Revocation:**
  - The forwarded assertion gains a signed `X-TS-Session-Issued-At` field, appended to the canonical string, so the gateway sends its session's creation time.
  - Core rejects (401) forwarded calls for users who are `disabled`, tombstoned, or whose `sessions_revoked_at` is later than the session's issue time.
  - Core sessions check the same three conditions.
  - A password change or reset, an MFA reset, disabling the user, or an admin "sign out everywhere" all set `sessions_revoked_at = now`.
- **Plex login** on any tier also refuses users who are disabled or tombstoned.

## Invite and reset flow

1. The admin (on core) creates a local user `{username, email?, permissions?, quotas?}` and receives `{user, invite_url}`.
   - `invite_url = APPLICATION_URL + "/invite/" + token`. `APPLICATION_URL` is the public gateway URL. If it is unset on core, return 503.
   - The raw token appears only in this one response.
2. The SPA route `/invite/:token` shows a "set your password" form.
3. `GET /api/auth/invite/{token}` returns `{username, purpose, expires_at}` or 404. `POST /api/auth/invite/{token}` with `{password}` sets the password, marks the token used and revokes sessions.
   - On the gateway, both are forwarded to core as the service principal and are on the forward allowlist.
   - They are rate-limited per IP: 10 per 15 min.
4. Admin "reset password" issues a `purpose=reset` token through the same endpoints. It also revokes sessions.

## Account self-service (any signed-in user; forwarded on the gateway)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/account` | `{id, username, auth_type, mfa_enabled, mfa_required, recovery_codes_remaining, quotas: {tracks, albums, discographies, window_days, used: {tracks, albums, discographies}}, auto_approve: {tracks, albums, discographies}}` |
| POST | `/api/account/password` | Local only. `{current_password, new_password}`. Revokes other sessions; the current gateway or core session gets re-issued. |
| POST | `/api/account/mfa/setup` | Local only. Body `{password}` (re-authentication): a wrong password returns 400 "Invalid password" and counts toward the account lockout (429 after 5). Returns `{secret, otpauth_uri}`. The secret is pending until confirmed, kept server-side for 10 min. |
| POST | `/api/account/mfa/confirm` | `{code}` → `{recovery_codes: string[]}` (shown once) |
| POST | `/api/account/mfa/disable` | `{password, code}`. Returns 409 if the admin requires MFA. |
| POST | `/api/account/mfa/recovery-codes` | `{password, code}` regenerates the codes. |

If `require_mfa_local` is on and a local user has no MFA, login succeeds with `{mfa_enrollment_required: true}`. Until enrollment, that session may only call `/api/account*` and `/api/auth/*`; everything else returns 403 `"mfa_enrollment_required"`.

## Admin user management (core only; `require_admin`; 404 on the gateway)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/users` | `[{id, username, email, auth_type, is_admin, permissions, disabled, mfa_enabled, last_login_at, created_at, quotas (effective + overrides), usage}]` |
| POST | `/api/admin/users` | Create a local user → `{user, invite_url}` |
| PATCH | `/api/admin/users/{id}` | `{permissions?, quota_tracks?, quota_albums?, quota_discographies?, quota_window_days?, email?}` (null resets to default) |
| POST | `/api/admin/users/{id}/disable` / `/enable` | Disable revokes sessions |
| POST | `/api/admin/users/{id}/reset-password` | Local only → `{reset_url}` |
| POST | `/api/admin/users/{id}/reset-mfa` | Clears TOTP and recovery codes, revokes sessions |
| POST | `/api/admin/users/{id}/revoke-sessions` | |
| DELETE | `/api/admin/users/{id}` | Body `{confirm_username}` must match. Deletes the user's data (FK cascade) and writes a tombstone. An admin can't delete themselves or the last admin. |
| POST | `/api/admin/users/{id}/restore` | Removes the tombstone (Plex users only), so they can sign in again |
| GET/PUT | `/api/admin/settings/accounts` | `{require_mfa_local, default_quota_*}` |

Rules:
- An admin cannot change their own admin bit, disable themselves, or demote the last admin.
- `ADMIN` can only be granted from core.
- Permission bits are shown with labels, and unknown bits are rejected.
- Today the existing `/api/users/{id}` PUT and `/refresh` routes overlap with these. Keep them working, but make them admin-only, or have them delegate to the same storage functions.

## Request quotas and approval

- Item types: `track`, `album`, `discography`.
  - A **discography request** is a batch of up to 50 albums of ONE artist, sent with `{"kind": "discography", "artist": "..."}` on `POST /api/requests/batch`.
  - It consumes 1 discography quota unit. Its albums do not consume album quota.
  - A batch without `kind` is a list of individual track and album requests. Each item consumes its own type's quota, with at most 50 items per batch.
- Quotas are counted over `quota_window_days` (rolling) for requests created by the user in any status except rejected/cancelled. The effective limit is the user override if set, otherwise the global default. Admins are unlimited.
- **Approval:** auto-approve is per type via permission bits.
  - Final mapping: `AUTO_APPROVE` (4) = tracks, `AUTO_APPROVE_ALBUM` (8) = albums, `AUTO_APPROVE_DISCOGRAPHY` (64) = discographies. Before migration v28, bit 4 approved every type, so v28 grants 8 and 64 to every user holding 4 and nobody loses approval.
  - Add `AUTO_APPROVE_DISCOGRAPHY = 64`.
  - Without the bit, the request is PENDING and waits for admin approval on core.
- All paths (single create, batch, mixes' auto-acquire) go through `request_submission.py` under `user_request_lock`. Responses are:
  - `400 "Request quota reached for albums (10 per 7 days)"`, with the same wording per type
  - `409` for duplicates

## Frontend

- **Login screen:** "Sign in with Plex" (the primary button) plus a "Sign in with username" form, with a TOTP step when `mfa_required` comes back. Lockout and 429 messages are generic.
- **`/invite/:token` page:**
  - shows the username and the expiry
  - password + confirm fields
  - live policy hints (length, not containing the username) and a strength meter that is purely a hint, since the server enforces the policy
  - on success, sends the user to the login page
- **Settings → Account (all users):**
  - username and sign-in type
  - quota usage bars per type, plus the auto-approve status
  - change password (local users)
  - MFA: setup shows the otpauth URI as a QR code if a QR library is already a dependency, otherwise the base32 secret plus a copyable URI; then confirm a code, then show the recovery codes once with copy and "I saved them"
  - disable MFA and regenerate recovery codes
- **Settings → Users (admin, core only):**
  - a table with search, a type badge, status (active/disabled) and an MFA badge
  - create local user, which shows the invite link once with copy and an expiry note
  - an edit drawer: a permissions checklist with labels, quota overrides with "use default", disable/enable, reset password (shows the link once), reset MFA, sign out everywhere, and delete (type the username to confirm)
- **Settings → Users → Defaults (admin):** default quotas, and the require-MFA switch.
- **Requests:** the user's remaining quota per type is visible, and the "Request discography" action on the artist view uses `kind: discography`. Check whether an artist "request all" exists today (`DiscoverView` / the artist detail in discovery).

## Gateway session enforcement

Core owns disable, delete, revoke and MFA-enrollment state. In the gateway role every session-authenticated request (gateway-local and forwarded) asks core `POST /api/internal/auth/session-status` (service principal only), body `{user_id, session_issued_at}` in epoch microseconds, reply `{valid: true}` or `{valid: false, reason: "disabled"|"deleted"|"revoked"|"mfa_enrollment_required"}`. Results are cached in memory for 60 s per `(user_id, session_issued_at)`. `valid=false` deletes the gateway session and returns 401 (`mfa_enrollment_required` returns 403 outside `/api/account` and `/api/auth`). If core cannot be reached or answers unexpectedly the gateway fails closed with 503 "Core unavailable". The gateway's Plex login asks core (uncached) before creating a session and refuses disabled or deleted users with the same 403 the core tier uses.
