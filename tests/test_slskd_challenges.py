"""slskd chat-challenge detection, routing, and admin reply/dismiss."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from trackseerr import slskd_challenges as sc
from trackseerr.models import ActiveDownload, DownloadClientConfig, DownloadDriverType, DownloadStatus
from trackseerr.storage import Database

REAL = (
    'Human check for your requested album "X". Reply only with this word / Antworte nur mit diesem Wort: '
    "NOTHIN. This unlocks downloads for 7 days. Valid for 2880 minutes. If this challenge expires, "
    "send !challenge to request a new one."
)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


class FakeDriver:
    def __init__(self, messages=None, send_ok=True):
        self.messages = messages if messages is not None else {}
        self.send_ok = send_ok
        self.cancelled, self.sent, self.acked = [], [], []

    def list_conversations(self):
        return [
            {"username": u, "hasUnAcknowledgedMessages": any(not m["acknowledged"] for m in ms)}
            for u, ms in self.messages.items()
        ]

    def get_conversation_messages(self, username):
        return self.messages.get(username, [])

    def cancel(self, download_hash):
        self.cancelled.append(download_hash)
        return True

    def send_private_message(self, username, text):
        self.sent.append((username, text))
        return self.send_ok

    def acknowledge_conversation(self, username):
        self.acked.append(username)
        return True


def _msg(text=REAL, direction="in", acknowledged=False, mid="1", username="Sharer"):
    return {"id": mid, "username": username, "message": text, "direction": direction,
            "timestamp": "2026-01-01T11:59:00Z", "acknowledged": acknowledged}


def _seed(db, sharer="sharer"):
    db.create_download_client(
        DownloadClientConfig(id="cl-1", name="slskd", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(id="dl-1", title="Artist - X", artist="Artist", client_id="cl-1",
                       download_hash=f"{sharer}::Music/X/a.flac", status="queued")
    )
    return {"id": "cl-1"}


class Research:
    def __init__(self):
        self.calls = []

    def __call__(self, db, item, reason):
        self.calls.append((item["id"], reason))


def _open_challenge(db, driver):
    cfg = _seed(db)
    sc.scan_client(db, cfg, driver, NOW, Research())
    return cfg, db.list_slskd_challenges()[0]


def test_looks_like_challenge_matches_real_message():
    assert sc.looks_like_challenge(REAL)


def test_looks_like_challenge_ignores_normal_chat():
    assert not sc.looks_like_challenge("hey, thanks for downloading, enjoy the music")


def test_challenge_expiry_from_minutes():
    assert sc.challenge_expiry(REAL, NOW) == NOW + timedelta(minutes=2880)
    assert sc.challenge_expiry("valid for 2 hours", NOW) == NOW + timedelta(hours=2)
    assert sc.challenge_expiry("no window here", NOW) is None


def test_scan_records_challenge_gates_peer_and_routes_around(test_db):
    cfg = _seed(test_db)
    admin = test_db.create_local_user("boss")[0]
    other = test_db.create_local_user("reg")[0]
    test_db.conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin["id"],))
    test_db.conn.commit()
    driver, research = FakeDriver({"Sharer": [_msg()]}), Research()
    assert sc.scan_client(test_db, cfg, driver, NOW, research) == 1
    row = test_db.get_active_download("dl-1")
    assert row["status"] == DownloadStatus.FAILED.value
    assert row["error_message"] == "Sharer asked for a chat reply before sharing. Trying another source."
    assert driver.cancelled == ["sharer::Music/X/a.flac"]
    assert len(research.calls) == 1
    assert test_db.list_blocklist() == []
    assert "sharer" in test_db.excluded_slskd_peers("cl-1", NOW.isoformat())
    chal = test_db.list_slskd_challenges()[0]
    assert chal["download_id"] == "dl-1" and chal["expires_at"] is not None
    notes = test_db.list_user_notifications(admin["id"])[0]
    assert [n["link"] for n in notes] == ["#/activity/review"]
    assert test_db.list_user_notifications(other["id"])[0] == []


def test_scan_is_idempotent(test_db):
    cfg = _seed(test_db)
    driver, research = FakeDriver({"Sharer": [_msg()]}), Research()
    assert sc.scan_client(test_db, cfg, driver, NOW, research) == 1
    # The download is failed now; re-activate it so the second scan can see the sharer again.
    test_db.update_download_status("dl-1", status=DownloadStatus.QUEUED.value)
    assert sc.scan_client(test_db, cfg, driver, NOW, research) == 0
    assert len(research.calls) == 1
    assert len(test_db.list_slskd_challenges(status=None)) == 1


def test_scan_ignores_messages_from_peers_without_downloads(test_db):
    cfg = _seed(test_db)
    driver, research = FakeDriver({"Stranger": [_msg(username="Stranger")]}), Research()
    assert sc.scan_client(test_db, cfg, driver, NOW, research) == 0
    assert research.calls == [] and driver.cancelled == []


def test_scan_ignores_outbound_and_acknowledged(test_db):
    cfg = _seed(test_db)
    driver = FakeDriver({"Sharer": [_msg(direction="out", mid="1"), _msg(acknowledged=True, mid="2"),
                                    _msg(text="thanks!", mid="3")]})
    driver.list_conversations = lambda: [{"username": "Sharer", "unAcknowledgedMessageCount": 1}]
    assert sc.scan_client(test_db, cfg, driver, NOW, Research()) == 0
    assert test_db.get_active_download("dl-1")["status"] == "queued"


def test_scan_does_not_regate_unlocked_peer(test_db):
    cfg = _seed(test_db)
    test_db.set_slskd_peer_state("cl-1", "Sharer", "unlocked", (NOW + timedelta(days=3)).isoformat())
    assert sc.scan_client(test_db, cfg, FakeDriver({"Sharer": [_msg()]}), NOW, Research()) == 1
    assert test_db.slskd_peer_state("cl-1", "sharer", NOW.isoformat()) == "unlocked"


def test_reply_sends_text_and_unlocks(test_db):
    driver = FakeDriver({"Sharer": [_msg()]})
    _, chal = _open_challenge(test_db, driver)
    research = Research()
    test_db.update_download_status("dl-1", status=DownloadStatus.QUEUED.value)
    with patch.object(sc, "get_acquisition_driver", return_value=driver):
        out = sc.reply_to_challenge(test_db, chal["id"], "  typed by admin  ", NOW, research)
    assert driver.sent == [("Sharer", "typed by admin")]
    assert out["status"] == "replied" and out["reply_text"] == "typed by admin"
    assert test_db.slskd_peer_state("cl-1", "sharer", NOW.isoformat()) == "unlocked"
    assert driver.acked == ["Sharer"]
    assert research.calls == [("dl-1", "sharer unlocked")]


def test_reply_rejects_empty_long_or_closed(test_db):
    driver = FakeDriver({"Sharer": [_msg()]})
    _, chal = _open_challenge(test_db, driver)
    with patch.object(sc, "get_acquisition_driver", return_value=driver):
        for bad in ("   ", "x" * 201):
            with pytest.raises(ValueError):
                sc.reply_to_challenge(test_db, chal["id"], bad, NOW, Research())
        with pytest.raises(LookupError):
            sc.reply_to_challenge(test_db, "nope", "a", NOW, Research())
        sc.dismiss_challenge(test_db, chal["id"])
        with pytest.raises(ValueError):
            sc.reply_to_challenge(test_db, chal["id"], "a", NOW, Research())
    assert driver.sent == []


def test_reply_raises_when_slskd_refuses(test_db):
    driver = FakeDriver({"Sharer": [_msg()]}, send_ok=False)
    _, chal = _open_challenge(test_db, driver)
    with patch.object(sc, "get_acquisition_driver", return_value=driver):
        with pytest.raises(RuntimeError, match="slskd did not accept"):
            sc.reply_to_challenge(test_db, chal["id"], "word", NOW, Research())
    assert test_db.get_slskd_challenge(chal["id"])["status"] == "open"


def test_dismiss_keeps_peer_gated(test_db):
    _, chal = _open_challenge(test_db, FakeDriver({"Sharer": [_msg()]}))
    assert sc.dismiss_challenge(test_db, chal["id"])["status"] == "dismissed"
    assert "sharer" in test_db.excluded_slskd_peers("cl-1", NOW.isoformat())
    with pytest.raises(LookupError):
        sc.dismiss_challenge(test_db, "nope")


def test_module_never_sends_without_reply_call(test_db):
    driver = FakeDriver({"Sharer": [_msg()]})
    _open_challenge(test_db, driver)
    assert driver.sent == [] and driver.acked == []
