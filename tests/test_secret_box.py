"""Tests for Fernet-based per-user credential sealing."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from trackseerr.secret_box import seal, unseal


def test_round_trip(tmp_path: Path) -> None:
    plaintext = "hunter2-s3cr3t!"
    token = seal(plaintext, data_dir=str(tmp_path))
    assert token != plaintext
    assert plaintext not in token
    assert unseal(token, data_dir=str(tmp_path)) == plaintext


def test_key_file_created_0600_and_reused(tmp_path: Path) -> None:
    token1 = seal("first-secret", data_dir=str(tmp_path))
    key_path = tmp_path / ".server_credentials_key"
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    original_key = key_path.read_bytes()

    token2 = seal("second-secret", data_dir=str(tmp_path))
    assert key_path.read_bytes() == original_key
    assert unseal(token1, data_dir=str(tmp_path)) == "first-secret"
    assert unseal(token2, data_dir=str(tmp_path)) == "second-secret"


def test_wrong_key_returns_none_and_omits_token_from_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = seal("my-password", data_dir=str(tmp_path))
    (tmp_path / ".server_credentials_key").write_bytes(Fernet.generate_key())

    with caplog.at_level(logging.WARNING):
        result = unseal(token, data_dir=str(tmp_path))

    assert result is None
    assert caplog.records
    assert token not in caplog.text
    assert "my-password" not in caplog.text


def test_missing_key_file_returns_none(tmp_path: Path) -> None:
    token = seal("my-password", data_dir=str(tmp_path))
    (tmp_path / ".server_credentials_key").unlink()
    assert unseal(token, data_dir=str(tmp_path)) is None


def test_empty_token_returns_none(tmp_path: Path) -> None:
    assert unseal("", data_dir=str(tmp_path)) is None


def test_unwritable_data_dir_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "access", lambda _path, _mode: False)
    with pytest.raises(PermissionError):
        seal("secret", data_dir=str(tmp_path))
