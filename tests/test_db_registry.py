"""register_db makes the explicitly provided Database authoritative; closed registry entries are not reused."""

import pytest

from trackseerr.api import dependencies as deps
from trackseerr.storage import Database


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "reg.sqlite"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr(deps, "_db_instances", {})
    yield path
    for inst in list(deps._db_instances.values()):
        inst.close()


def test_register_db_replaces_instance_created_by_get_db(db_path):
    first = deps.get_db()
    explicit = Database(db_path)
    deps.register_db(explicit)
    assert deps.get_db() is explicit
    assert deps.get_db() is not first
    # The replaced instance is left open for any holder.
    assert first._conn is not None
    first.close()
    explicit.close()


def test_register_db_same_instance_is_noop(db_path):
    db = Database(db_path)
    deps.register_db(db)
    deps.register_db(db)
    assert deps.get_db() is db
    db.close()


def test_get_db_recreates_closed_registered_instance(db_path):
    db = deps.get_db()
    db.close()
    fresh = deps.get_db()
    assert fresh is not db
    assert fresh._conn is not None
