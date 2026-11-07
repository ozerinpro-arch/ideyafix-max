"""Explicit private loopback profile, separate from destructive test fixtures."""
import os
from pathlib import Path
import stat

from scripts import school_local_storage as storage


def private_local_database_paths():
    if (os.environ.get('SHKOLA_LOCAL_RUNTIME_MODE') != 'persistent'
            or os.environ.get('SHKOLA_WORKSPACE_MODE') != 'school'
            or os.environ.get('SHKOLA_SCHOOL_ID') != 'shkola-real-local-demo'
            or os.environ.get('SHKOLA_TEST_MODE') == 'isolated'
            or os.environ.get('SHKOLA_REAL_ALLOW_TEMPORARY') == '1'):
        raise ValueError('Explicit private local-school profile required')
    storage._directory(storage.STORAGE_ROOT)
    raw = os.environ.get('SHKOLA_REAL_RUNTIME_DIR')
    if not raw:
        raise ValueError('Explicit persistent local-school directory required')
    runtime = storage.validate_runtime(raw)
    identities, paths = set(), []
    for variable, name in [('SHKOLA_DB_PATH', 'domain.sqlite3'),
                           ('AGENT_DB_PATH', 'agent.sqlite3'),
                           ('SHKOLA_STAFF_DB_PATH', 'staff.sqlite3'),
                           ('SHKOLA_REQUEST_AI_DB_PATH', 'request-ai.sqlite3')]:
        value = os.environ.get(variable)
        if not value or Path(value) != runtime / name:
            raise ValueError('Local databases must use distinct canonical private runtime files')
        path = Path(value)
        if path.is_symlink():
            raise ValueError('Local database symlinks are forbidden')
        if path.exists():
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or identity in identities):
                raise ValueError('Private owned distinct regular0600 local databases required')
            identities.add(identity)
        paths.append(path)
    return tuple(paths)
