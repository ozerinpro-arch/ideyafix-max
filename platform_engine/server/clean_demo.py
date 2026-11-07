"""Offline, guarded cleanup. No HTTP route; operator creates a backup first."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .seed import seed_state
from .workspace import COLLECTIONS, empty_school_state


def table_fingerprints(connection: sqlite3.Connection) -> dict:
    """Fingerprint domain tables without exposing their content in a report."""
    result = {}
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    for (name,) in tables:
        if name == 'demo_state':
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        rows = connection.execute('SELECT * FROM ' + quoted + ' ORDER BY rowid').fetchall()
        value = json.dumps(rows, ensure_ascii=False, default=lambda item: {'bytes': item.hex()}).encode()
        result[name] = {'rows': len(rows), 'sha256': hashlib.sha256(value).hexdigest()}
    return result


def clean_database(path: Path, expected_payload_sha256: str, *, dry_run: bool = True) -> dict:
    """Remove only the exact known seed, refusing modified or unknown records."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('Explicit existing regular database required')
    if not re.fullmatch(r'[a-f0-9]{64}', expected_payload_sha256):
        raise ValueError('Expected payload fingerprint required')
    with closing(sqlite3.connect(path, isolation_level=None, timeout=15)) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            row = connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()
            if row is None or hashlib.sha256(row[0].encode()).hexdigest() != expected_payload_sha256:
                raise ValueError('Workspace changed; cleanup refused')
            state = json.loads(row[0])
            if state.get('workspaceMode') == 'school':
                connection.rollback()
                return {'changed': False, 'dryRun': dry_run, 'alreadyClean': True, 'removed': 0}
            baseline = seed_state()
            if '_finance' in state:
                from .finance import ensure_finance
                ensure_finance(baseline)
            if state != baseline:
                raise ValueError('Unknown or edited records; cleanup refused')
            before = table_fingerprints(connection)
            removed = sum(len(state.get(key, [])) for key in COLLECTIONS)
            removed += len(state.get('_finance', {}).get('invoices', []))
            cleaned = json.dumps(empty_school_state(), ensure_ascii=False)
            if not dry_run:
                result = connection.execute('UPDATE demo_state SET payload=? WHERE id=1 AND payload=?', (cleaned, row[0]))
                if result.rowcount != 1:
                    raise ValueError('Concurrent change; cleanup refused')
            after = table_fingerprints(connection)
            if before != after:
                raise ValueError('Domain tables changed; cleanup refused')
            if dry_run:
                connection.rollback()
            else:
                connection.commit()
            return {'changed': not dry_run, 'dryRun': dry_run, 'removed': removed,
                    'domainTablesUnchanged': True, 'tables': after}
        except BaseException:
            connection.rollback()
            raise
