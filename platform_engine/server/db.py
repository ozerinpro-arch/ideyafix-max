"""Atomic domain storage; school workspaces never auto-seed synthetic records."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .seed import seed_state
from .workspace import empty_school_state, school_workspace, verify_school_state

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "demo.sqlite3"
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


def database_path() -> Path:
    return Path(os.environ.get("SHKOLA_DB_PATH", str(DEFAULT_DB_PATH))).resolve()


def table_exists(connection, name: str) -> bool:
    if connection is None:
        return False
    postgres_lookup = getattr(connection, 'table_exists', None)
    if postgres_lookup is not None:
        return postgres_lookup(name)
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("CREATE TABLE IF NOT EXISTS demo_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
    initial = empty_school_state() if school_workspace() else seed_state()
    connection.execute("INSERT OR IGNORE INTO demo_state(id,payload) VALUES(1,?)", (json.dumps(initial, ensure_ascii=False),))
    connection.commit()
    return connection


def read_state() -> dict:
    connection = connect()
    try:
        state = json.loads(connection.execute("SELECT payload FROM demo_state WHERE id=1").fetchone()[0])
        verify_school_state(state)
        return state
    finally:
        connection.close()


@contextmanager
def edit_state():
    """Yield mutable state under an exclusive transaction; exceptions roll back."""
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = json.loads(connection.execute("SELECT payload FROM demo_state WHERE id=1").fetchone()[0])
        verify_school_state(state)
        yield state
        connection.execute("UPDATE demo_state SET payload=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def reset_state() -> dict:
    if school_workspace():
        raise ValueError('Demo reset is unavailable in a school workspace')
    with edit_state() as state:
        state.clear()
        state.update(seed_state())
    return state
