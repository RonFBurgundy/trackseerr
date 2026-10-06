"""Outbound notification engine for TrackSeerr.

Supports Discord, Telegram, Pushover, generic Webhooks, and Email (SMTP),
dispatched asynchronously via background threads to prevent blocking API responses.
"""

from datetime import datetime, timezone
from email.message import EmailMessage
import html
import logging
import smtplib
import ssl
import threading
from typing import Any, Optional, Union

import httpx

from plex_playlist_sync.models import NotificationChannelType, NotificationEvent
from plex_playlist_sync.security import is_safe_service_url
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


def format_notification(event: str, data: dict[str, Any]) -> tuple[str, str]:
    """Generates user-friendly title and message for a given notification event."""
    artist = str(data.get("artist") or "Unknown Artist")
    title = str(data.get("title") or "Unknown Title")
    item_type = str(data.get("item_type") or "music")
    username = data.get("username")
    album = data.get("album")

    if event == NotificationEvent.REQUEST_CREATED.value:
        subj = f"Music Requested: {artist} - {title}"
        body = f"{username or 'A user'} requested {item_type} '{artist} - {title}'"
        if album:
            body += f" (Album: {album})"
    elif event == NotificationEvent.REQUEST_APPROVED.value:
        subj = f"Request Approved: {artist} - {title}"
        body = f"Request for {item_type} '{artist} - {title}' was approved."
    elif event == NotificationEvent.REQUEST_REJECTED.value:
        subj = f"Request Rejected: {artist} - {title}"
        body = f"Request for {item_type} '{artist} - {title}' was rejected."
    elif event == NotificationEvent.DOWNLOAD_STARTED.value:
        client = data.get("client")
        release = data.get("release") or title
        subj = f"Download Started: {artist} - {title}"
        body = f"Grabbed release '{release}'"
        if client:
            body += f" via download client '{client}'."
        else:
            body += "."
    elif event == NotificationEvent.ITEM_AVAILABLE.value:
        subj = f"Music Available: {artist} - {title} is ready in Plex!"
        body = f"'{artist} - {title}' has been imported and is now ready in your Plex library."
    elif event == NotificationEvent.DOWNLOAD_FAILED.value:
        err = data.get("error_message") or data.get("error") or "Unknown error"
        subj = f"Download Failed: {artist} - {title}"
        body = f"Failed to download/import '{artist} - {title}': {err}."
    elif event == NotificationEvent.ISSUE_REPORTED.value:
        issue_type = data.get("issue_type") or "Issue"
        details = data.get("problem_details") or ""
        subj = f"Issue Reported: {artist} - {title} ({issue_type})"
        body = f"{username or 'A user'} reported an issue ({issue_type}) for '{artist} - {title}': {details}"
    elif event == NotificationEvent.ISSUE_UPDATED.value:
        issue_type = data.get("issue_type") or "Issue"
        update = data.get("update") or "Issue updated"
        subj = f"Issue Updated: {artist} - {title} ({issue_type})"
        body = f"{update} on the {issue_type} issue for '{artist} - {title}' (status: {data.get('status') or 'unknown'})."
    elif event == NotificationEvent.ISSUE_RESOLVED.value:
        issue_type = data.get("issue_type") or "Issue"
        outcome = "closed as won't fix" if data.get("status") == "wont_fix" else "resolved"
        subj = f"Issue {outcome.capitalize()}: {artist} - {title} ({issue_type})"
        body = f"The {issue_type} issue for '{artist} - {title}' was {outcome}."
    else:
        subj = f"TrackSeerr Notification: {event}"
        body = f"Notification event '{event}' for '{artist} - {title}'."

    return subj, body


class NotificationDispatcher:
    """Manages notification channels, background dispatching, and live test delivery."""

    def format_notification(self, event: str, data: dict[str, Any]) -> tuple[str, str]:
        """Exposes title/message formatting on the dispatcher instance."""
        return format_notification(event, data)

    def _send_discord(
        self,
        webhook_url: str,
        title: str,
        message: str,
        data: dict[str, Any],
        event: Optional[str] = None,
    ) -> None:
        """Dispatches an embedded notification payload to a Discord webhook."""
        if not is_safe_service_url(webhook_url, allow_lan=True):
            raise ValueError(f"Prohibited Discord webhook URL (SSRF protection): '{webhook_url}'")

        # Color: green for available, blue for requested/approved/download_started, red for failed/rejected/issue
        if event in (NotificationEvent.ITEM_AVAILABLE.value, "available"):
            color = 0x2ECC71  # Green
        elif event in (
            NotificationEvent.DOWNLOAD_FAILED.value,
            NotificationEvent.REQUEST_REJECTED.value,
            "failed",
        ):
            color = 0xE74C3C  # Red
        elif event in (
            NotificationEvent.ISSUE_REPORTED.value,
            NotificationEvent.ISSUE_UPDATED.value,
            "issue_reported",
        ):
            color = 0xE67E22  # Orange
        elif event == NotificationEvent.ISSUE_RESOLVED.value:
            color = 0x2ECC71  # Green
        else:
            color = 0x3498DB  # Blue

        embed: dict[str, Any] = {
            "title": title[:256],
            "description": message[:2048],
            "color": color,
            "fields": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if data.get("artist"):
            embed["fields"].append({"name": "Artist", "value": str(data["artist"]), "inline": True})
        if data.get("title"):
            embed["fields"].append({"name": "Title", "value": str(data["title"]), "inline": True})
        if data.get("album"):
            embed["fields"].append({"name": "Album", "value": str(data["album"]), "inline": True})
        if data.get("issue_type"):
            embed["fields"].append({"name": "Issue Type", "value": str(data["issue_type"]), "inline": True})
        if data.get("username"):
            embed["fields"].append({"name": "Reported By" if event in (NotificationEvent.ISSUE_REPORTED.value, NotificationEvent.ISSUE_UPDATED.value, NotificationEvent.ISSUE_RESOLVED.value) else "Requested By", "value": str(data["username"]), "inline": True})
        if data.get("problem_details"):
            embed["fields"].append({"name": "Details", "value": str(data["problem_details"])[:1024], "inline": False})
        if data.get("client"):
            embed["fields"].append({"name": "Client", "value": str(data["client"]), "inline": True})
        if data.get("application_url"):
            embed["url"] = str(data["application_url"])
            embed["fields"].append({"name": "TrackSeerr", "value": f"[Open TrackSeerr]({data['application_url']})", "inline": True})
        if data.get("cover_url"):
            embed["thumbnail"] = {"url": str(data["cover_url"])}

        payload = {
            "username": "TrackSeerr",
            "embeds": [embed],
        }

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(webhook_url, json=payload)
            resp.raise_for_status()

    def _send_telegram(
        self,
        bot_token: str,
        chat_id: str,
        title: str,
        message: str,
        data: dict[str, Any],
    ) -> None:
        """Dispatches an HTML-formatted message to the Telegram Bot API."""
        if not bot_token or not chat_id:
            raise ValueError("Telegram configuration must contain 'bot_token' and 'chat_id'")

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        escaped_title = html.escape(title)
        escaped_message = html.escape(message)
        text = f"<b>{escaped_title}</b>\n{escaped_message}"
        if data.get("application_url"):
            app_url = html.escape(str(data["application_url"]))
            text += f'\n\n<a href="{app_url}">Open in TrackSeerr</a>'

        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
        }

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()

    def _send_pushover(
        self,
        user_key: str,
        app_token: str,
        title: str,
        message: str,
        data: dict[str, Any],
    ) -> None:
        """Dispatches a push notification via the Pushover REST API."""
        if not user_key or not app_token:
            raise ValueError("Pushover configuration must contain 'user_key' and 'app_token'")

        url = "https://api.pushover.net/1/messages.json"
        payload = {
            "token": str(app_token),
            "user": str(user_key),
            "title": title[:250],
            "message": message[:1024],
        }
        if data.get("application_url"):
            payload["url"] = str(data["application_url"])
            payload["url_title"] = "Open in TrackSeerr"

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, data=payload)
            resp.raise_for_status()

    def _send_webhook(
        self,
        webhook_url: str,
        event: str,
        data: dict[str, Any],
        secret_header: Optional[str] = None,
    ) -> None:
        """Dispatches an HTTP POST webhook containing the complete event payload."""
        if not is_safe_service_url(webhook_url, allow_lan=True):
            raise ValueError(f"Prohibited webhook URL (SSRF protection): '{webhook_url}'")

        headers = {"Content-Type": "application/json"}
        if secret_header:
            headers["X-TrackSeerr-Secret"] = str(secret_header)

        payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        if data.get("application_url"):
            payload["application_url"] = str(data["application_url"])

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(webhook_url, json=payload, headers=headers)
            resp.raise_for_status()

    def _send_email(
        self,
        smtp_host: str,
        smtp_port: int,
        username: Optional[str],
        password: Optional[str],
        use_tls: bool,
        use_ssl: bool,
        from_addr: str,
        to_addr: str,
        title: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """Dispatches an email notification via standard library SMTP."""
        if not smtp_host:
            raise ValueError("Missing 'smtp_host' in Email configuration")
        if not from_addr:
            raise ValueError("Missing 'from_addr' in Email configuration")
        if not to_addr:
            raise ValueError("Missing 'to_addr' in Email configuration")

        body = message
        if data and data.get("application_url"):
            body += f"\n\nOpen in TrackSeerr: {data['application_url']}"

        msg = EmailMessage()
        msg["Subject"] = f"[TrackSeerr] {title}"
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.set_content(body)

        port = int(smtp_port) if smtp_port else (465 if use_ssl else 587)

        if use_ssl or port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, port, context=context, timeout=10.0) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=10.0) as server:
                if use_tls:
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                if username and password:
                    server.login(username, password)
                server.send_message(msg)

    def _send_to_channel(
        self,
        channel: dict[str, Any],
        event: str,
        data: dict[str, Any],
    ) -> None:
        """Routes notification payload to the matching driver for a configured channel."""
        channel_type = str(channel.get("channel_type", "")).lower().strip()
        config = channel.get("config") or {}
        title, message = self.format_notification(event, data)

        if channel_type == NotificationChannelType.DISCORD.value:
            webhook_url = config.get("webhook_url")
            if not webhook_url:
                raise ValueError("Missing 'webhook_url' in Discord configuration")
            self._send_discord(
                webhook_url=webhook_url,
                title=title,
                message=message,
                data=data,
                event=event,
            )
        elif channel_type == NotificationChannelType.TELEGRAM.value:
            bot_token = config.get("bot_token")
            chat_id = config.get("chat_id")
            if not bot_token or not chat_id:
                raise ValueError("Missing 'bot_token' or 'chat_id' in Telegram configuration")
            self._send_telegram(
                bot_token=bot_token,
                chat_id=chat_id,
                title=title,
                message=message,
                data=data,
            )
        elif channel_type == NotificationChannelType.PUSHOVER.value:
            user_key = config.get("user_key")
            app_token = config.get("app_token") or config.get("token")
            if not user_key or not app_token:
                raise ValueError("Missing 'user_key' or 'app_token' in Pushover configuration")
            self._send_pushover(
                user_key=user_key,
                app_token=app_token,
                title=title,
                message=message,
                data=data,
            )
        elif channel_type == NotificationChannelType.WEBHOOK.value:
            webhook_url = config.get("webhook_url")
            secret_header = config.get("secret_header") or config.get("secret")
            if not webhook_url:
                raise ValueError("Missing 'webhook_url' in Webhook configuration")
            self._send_webhook(
                webhook_url=webhook_url,
                event=event,
                data=data,
                secret_header=secret_header,
            )
        elif channel_type == NotificationChannelType.EMAIL.value:
            smtp_host = config.get("smtp_host")
            smtp_port = int(config.get("smtp_port") or 587)
            username = config.get("username")
            password = config.get("password")
            use_tls = bool(config.get("use_tls", True))
            use_ssl = bool(config.get("use_ssl", False))
            from_addr = config.get("from_addr") or config.get("from_email")
            to_addr = config.get("to_addr") or config.get("to_email")
            self._send_email(
                smtp_host=smtp_host or "",
                smtp_port=smtp_port,
                username=username,
                password=password,
                use_tls=use_tls,
                use_ssl=use_ssl,
                from_addr=from_addr or "",
                to_addr=to_addr or "",
                title=title,
                message=message,
                data=data,
            )
        else:
            raise ValueError(f"Unsupported notification channel type '{channel_type}'")

    def _run_dispatch(
        self,
        event: str,
        data: dict[str, Any],
        db: Optional[Database] = None,
    ) -> None:
        """Worker task executing channel sends in an isolated background thread."""
        try:
            database = db
            if database is None:
                try:
                    from plex_playlist_sync.api.dependencies import get_db

                    database = get_db()
                except Exception as e:
                    logger.warning("Could not resolve database instance for notification dispatch: %s", e)
                    return

            if not data.get("application_url"):
                try:
                    if database is not None:
                        gen_cfg = database.get_general_settings()
                        if gen_cfg.get("application_url"):
                            data["application_url"] = gen_cfg["application_url"]
                except Exception as e:
                    logger.debug("Could not resolve application_url for notification: %s", e)
            if not data.get("application_url"):
                import os
                env_url = (os.getenv("APPLICATION_URL") or os.getenv("APP_URL") or "").strip().rstrip("/")
                if env_url:
                    data["application_url"] = env_url

            channels = database.list_notification_channels(enabled_only=True)
        except Exception as e:
            logger.warning("Failed to query notification channels for event '%s': %s", event, e)
            return

        for ch in channels:
            try:
                events = ch.get("events") or []
                if event not in events:
                    continue
                self._send_to_channel(ch, event, data)
                logger.info(
                    "Successfully delivered notification '%s' to channel '%s' (%s)",
                    event,
                    ch.get("name"),
                    ch.get("channel_type"),
                )
            except (httpx.HTTPError, httpx.TimeoutException, smtplib.SMTPException, ValueError) as e:
                logger.warning(
                    "Notification delivery failed for channel '%s' (%s) on event '%s': %s",
                    ch.get("name"),
                    ch.get("channel_type"),
                    event,
                    e,
                )
            except Exception as e:
                logger.warning(
                    "Unexpected error delivering notification to channel '%s' on event '%s': %s",
                    ch.get("name"),
                    event,
                    e,
                )

    def dispatch(
        self,
        event: Union[str, NotificationEvent],
        data: dict[str, Any],
        db: Optional[Database] = None,
    ) -> None:
        """Asynchronously dispatches an event notification to all eligible channels."""
        event_str = event.value if isinstance(event, NotificationEvent) else str(event)
        data_copy = dict(data)
        t = threading.Thread(
            target=self._run_dispatch,
            args=(event_str, data_copy, db),
            daemon=True,
            name=f"Notify-{event_str}",
        )
        t.start()

    def test_channel(self, channel_type: str, config: dict[str, Any]) -> tuple[bool, str]:
        """Synchronously tests a channel configuration with a synthetic test event."""
        synthetic_data = {
            "artist": "TrackSeerr Test Artist",
            "title": "Notification Test Track",
            "album": "Test Album",
            "username": "admin",
            "item_type": "track",
        }
        import os

        env_url = (os.getenv("APPLICATION_URL") or os.getenv("APP_URL") or "").strip().rstrip("/")
        if env_url:
            synthetic_data["application_url"] = env_url

        dummy_channel = {
            "id": "test-channel",
            "name": "Live Test Channel",
            "channel_type": channel_type,
            "config": config,
        }
        try:
            self._send_to_channel(dummy_channel, "test", synthetic_data)
            return True, "Notification sent successfully"
        except (httpx.HTTPError, httpx.TimeoutException, smtplib.SMTPException, ValueError) as e:
            logger.warning("Live test delivery failed for channel type '%s': %s", channel_type, e)
            return False, str(e)
        except Exception as e:
            logger.warning("Unexpected error during live test delivery for '%s': %s", channel_type, e)
            return False, str(e)


# Global notification dispatcher singleton
notification_dispatcher = NotificationDispatcher()

__all__ = [
    "NotificationDispatcher",
    "notification_dispatcher",
    "format_notification",
]
