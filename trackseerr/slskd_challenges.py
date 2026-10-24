"""Detection and routing of slskd sharer "human check" chat challenges.

Some Soulseek sharers hold transfers until a human answers a private message. Trackseerr never answers on its own:
this module only detects and records the challenge, notifies admins, stops waiting on that sharer and keeps it out of
searches. The answer is typed by an admin and sent verbatim by :func:`reply_to_challenge`. Nothing here extracts,
parses, guesses or prefills the answer.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from trackseerr.clients.acquisition import get_acquisition_driver
from trackseerr.models import DownloadStatus
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

GATE_DAYS = 7
UNLOCK_DAYS = 7
SCAN_INTERVAL_SECONDS = 60
MAX_REPLY_CHARS = 200

_MARKERS = ("human check", "reply only with", "antworte nur", "!challenge", "captcha", "verification word")
_VALIDITY_RE = re.compile(r"valid\s+for\s+(\d+)\s*(minutes?|hours?)", re.IGNORECASE)

ResearchFn = Callable[[Database, dict[str, Any], str], None]


def looks_like_challenge(text: str) -> bool:
    """True when the message contains any challenge marker (case-insensitive)."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _MARKERS)


def challenge_expiry(text: str, received_at: datetime) -> Optional[datetime]:
    """When the challenge lapses, from its ``valid for <N> minutes|hours`` window; None if not stated."""
    match = _VALIDITY_RE.search(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    delta = timedelta(hours=amount) if unit.startswith("hour") else timedelta(minutes=amount)
    return received_at + delta


def _sharer_of(download_hash: Any) -> str:
    return str(download_hash or "").split("::", 1)[0].strip().lower()


def _has_unread(conversation: dict[str, Any]) -> bool:
    if conversation.get("hasUnAcknowledgedMessages"):
        return True
    try:
        return int(conversation.get("unAcknowledgedMessageCount") or 0) > 0
    except (TypeError, ValueError):
        return False


def _parse_timestamp(raw: str, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=fallback.tzinfo)


def scan_client(
    db: Database, cfg: dict[str, Any], driver: Any, now: datetime, research: ResearchFn
) -> int:
    """Scans one slskd client's chat for challenges from sharers we are downloading from; returns new challenges."""
    client_id = str(cfg.get("id"))
    now_iso = now.isoformat()
    db.expire_slskd_challenges(now_iso)

    items_by_sharer: dict[str, dict[str, Any]] = {}
    active = db.list_active_downloads(
        statuses=[DownloadStatus.QUEUED.value, DownloadStatus.DOWNLOADING.value]
    )
    for item in active:
        if str(item.get("client_id")) != client_id:
            continue
        sharer = _sharer_of(item.get("download_hash"))
        if sharer:
            items_by_sharer.setdefault(sharer, item)
    if not items_by_sharer:
        return 0

    new_count = 0
    for conversation in driver.list_conversations():
        username = str(conversation.get("username") or "").strip()
        item = items_by_sharer.get(username.lower())
        if not username or item is None or not _has_unread(conversation):
            continue
        for message in driver.get_conversation_messages(username):
            if message.get("direction") != "in" or message.get("acknowledged"):
                continue
            text = message.get("message") or ""
            if not looks_like_challenge(text):
                continue
            received = _parse_timestamp(message.get("timestamp") or "", now)
            expires = challenge_expiry(text, received)
            row = db.record_slskd_challenge(
                client_id,
                username,
                str(message.get("id")),
                text,
                received.isoformat(),
                expires_at=expires.isoformat() if expires else None,
                download_id=item["id"],
                download_title=item.get("title"),
            )
            if row is None:
                continue
            new_count += 1
            if db.slskd_peer_state(client_id, username, now_iso) != "unlocked":
                db.set_slskd_peer_state(client_id, username, "gated", (now + timedelta(days=GATE_DAYS)).isoformat())
            _route_around(db, driver, item, username, research)
            _notify_admins(db, row)
    return new_count


def _route_around(db: Database, driver: Any, item: dict[str, Any], username: str, research: ResearchFn) -> None:
    """Stops waiting on the sharer: cancels and fails the download (no blocklisting) and searches elsewhere."""
    if not driver.cancel(item["download_hash"]):
        logger.warning("slskd did not cancel the transfer %s held by %s's challenge", item["download_hash"], username)
    message = f"{username} asked for a chat reply before sharing. Trying another source."
    db.update_download_status(item["id"], status=DownloadStatus.FAILED.value, error_message=message)
    db.record_event(
        "download_failed",
        f"{item.get('title') or item['id']}: {message}",
        source="AcquisitionWorker",
        severity="warning",
        details={"download_id": item["id"], "username": username},
    )
    research(db, item, f"{username} asked for a chat reply")


def _notify_admins(db: Database, row: dict[str, Any]) -> None:
    for user in db.list_users():
        if not user.get("is_admin") or user.get("disabled"):
            continue
        try:
            db.create_user_notification(
                user["id"],
                "slskd_challenge",
                "A Soulseek sharer asked for a reply",
                f"{row['username']} wants a chat reply before sharing \"{row['download_title'] or 'a download'}\". "
                "Answer it in Activity → Needs review.",
                link="#/activity/review",
            )
        except sqlite3.Error as exc:
            logger.warning("Could not notify admin %s of a slskd challenge: %s", user.get("id"), safe_exc(exc))


def reply_to_challenge(
    db: Database, challenge_id: str, text: str, now: datetime, research: ResearchFn
) -> dict[str, Any]:
    """Sends the admin-typed ``text`` verbatim to the sharer and unlocks it. The caller supplies the text."""
    row = db.get_slskd_challenge(challenge_id)
    if row is None:
        raise LookupError(f"Unknown slskd challenge: {challenge_id}")
    if row["status"] != "open":
        raise ValueError(f"Challenge is {row['status']}, not open")
    reply = text.strip()
    if not reply:
        raise ValueError("Reply must not be empty")
    if len(reply) > MAX_REPLY_CHARS:
        raise ValueError(f"Reply must be at most {MAX_REPLY_CHARS} characters")
    cfg = db.get_download_client(row["client_id"])
    if cfg is None:
        raise LookupError(f"Download client {row['client_id']} no longer exists")
    driver = get_acquisition_driver(cfg)
    username = row["username"]
    if not driver.send_private_message(username, reply):
        raise RuntimeError("slskd did not accept the message")
    updated = db.set_slskd_challenge_status(challenge_id, "replied", reply_text=reply) or row
    db.set_slskd_peer_state(row["client_id"], username, "unlocked", (now + timedelta(days=UNLOCK_DAYS)).isoformat())
    if not driver.acknowledge_conversation(username):
        logger.warning("slskd did not mark the conversation with %s as read", username)
    item = db.get_active_download(row["download_id"]) if row.get("download_id") else None
    if item is not None:
        research(db, item, "sharer unlocked")
    return updated


def dismiss_challenge(db: Database, challenge_id: str) -> dict[str, Any]:
    """Dismisses a challenge without answering; the sharer stays gated until its gate lapses."""
    updated = db.set_slskd_challenge_status(challenge_id, "dismissed")
    if updated is None:
        raise LookupError(f"Unknown slskd challenge: {challenge_id}")
    return updated
