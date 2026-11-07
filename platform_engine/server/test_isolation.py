"""Shared fail-closed gate for destructive synthetic test fixtures.

This prevents accidental use of a working database; it is not a filesystem
sandbox against a hostile process running under the same OS account.
"""
import os
from pathlib import Path
import tempfile
import sys


def isolated_database_paths() -> tuple[Path, Path]:
    if os.environ.get("SHKOLA_TEST_MODE") != "isolated":
        raise ValueError("Explicit isolated test mode is required")
    # TMPDIR is user-controlled: it must not expand the allowlist into a project.
    aliases = [Path("/tmp"), Path("/var/tmp")]
    system_temp = Path(tempfile.gettempdir()).absolute()
    resolved_temp = system_temp.resolve()
    known_roots = [root.resolve() for root in aliases]
    if sys.platform == "darwin":
        known_roots.append(Path("/private/var/folders"))
    if system_temp.is_dir() and any(resolved_temp.is_relative_to(root) for root in known_roots):
        aliases.append(system_temp)
    roots = tuple(dict.fromkeys([*aliases, *(root.resolve() for root in aliases)]))
    paths, identities = [], set()
    try:
        for variable in ("SHKOLA_DB_PATH", "AGENT_DB_PATH"):
            value = os.environ.get(variable)
            if not value or not Path(value).is_absolute():
                raise ValueError("Both absolute temporary database paths are required")
            raw, accepted = Path(value), False
            for root in roots:
                if not raw.is_relative_to(root):
                    continue
                relative = raw.relative_to(root)
                if not relative.parts or ".." in relative.parts:
                    continue
                current = root
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError("Database paths cannot contain symbolic links")
                resolved = raw.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise ValueError("Database paths must remain temporary")
                accepted = True
                break
            if not accepted:
                raise ValueError("Database paths must be inside a system temporary directory")
            if resolved.exists():
                if not resolved.is_file():
                    raise ValueError("Database paths must be regular files")
                info = resolved.stat()
                identity = (info.st_dev, info.st_ino)
                if info.st_nlink != 1 or identity in identities:
                    raise ValueError("Linked or shared database files are forbidden")
                identities.add(identity)
            paths.append(resolved)
    except OSError:
        raise ValueError("Temporary database paths cannot be verified") from None
    if paths[0] == paths[1]:
        raise ValueError("Domain and agent databases must be distinct")
    return paths[0], paths[1]
