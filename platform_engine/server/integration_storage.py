"""Private, separate operational sidecars. Never migrate a school/access database."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat

APPLICATION_ID = 0x53434849


def validate_private_path(raw, forbidden=()):
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Нужен отдельный абсолютный путь хранилища подключений')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('Ссылки в пути хранилища подключений запрещены')
    # Current host paths are a second fence against accidental configuration.
    others = list(forbidden) + [os.environ.get(k) for k in
        ('SHKOLA_DB_PATH', 'AGENT_DB_PATH', 'SHKOLA_STAFF_DB_PATH')]
    for other in filter(None, others):
        other = Path(other)
        if path.resolve() == other.resolve() or (path.exists() and other.exists() and os.path.samefile(path, other)):
            raise ValueError('Подключения требуют отдельного хранилища')
    if path.exists():
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_nlink != 1):
            raise ValueError('Нужен собственный обычный файл хранилища с правами 0600')
    return path


def _check_schema(connection, allowed_tables):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        raise ValueError('Неизвестный формат хранилища подключений')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    app_id = connection.execute('PRAGMA application_id').fetchone()[0]
    if not tables <= set(allowed_tables) | {'sqlite_sequence'} or app_id not in {0, APPLICATION_ID}:
        raise ValueError('Неизвестный формат хранилища подключений')
    if tables and app_id != APPLICATION_ID:
        raise ValueError('Файл не является хранилищем подключений')


def open_private_sqlite(raw, allowed_tables, *, readonly=False, forbidden=()):
    """Return a connection; an absent readonly file returns None without creation.

    Callers declare their entire schema allowlist, then create only that schema.
    Wrong permissions/schema are rejected, never repaired in place.
    """
    path = validate_private_path(raw, forbidden)
    if readonly and not path.exists():
        return None
    if not readonly:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        validate_private_path(path, forbidden)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    validate_private_path(path, forbidden)
    before = path.stat()
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=15)
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError('Хранилище изменилось во время открытия')
        _check_schema(connection, allowed_tables)
        if not readonly and connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.commit()
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise
