"""Explicit private file configuration for the separately enabled real MAX worker."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

@dataclass(frozen=True, repr=False)
class MaxSettings:
    enabled: bool
    bot_username: str
    token_file: Path
    ca_file: Path | None
    db_path: Path
    activated_at_ms: int = 0
    platform_enabled: bool = False
    nanogpt_config: Path | None = None

    def __repr__(self):
        return '<MaxSettings credentials=redacted>'

    @classmethod
    def load(cls, filename):
        path = Path(filename)
        if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('invalid_private_max_config')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 8192:
            raise ValueError('invalid_private_max_config')
        try:
            value = json.loads(path.read_bytes())
            required = {'enabled', 'botUsername', 'tokenFile', 'caFile', 'dbPath', 'activatedAtMs'}
            if type(value) is not dict or not required <= set(value) or set(value)-required-{'platformEnabled','nanogptConfig'} or type(value['enabled']) is not bool:
                raise ValueError()
            if type(value.get('platformEnabled',False))is not bool:raise ValueError()
            nanogpt=value.get('nanogptConfig')
            if nanogpt is not None and (not value.get('platformEnabled',False)or type(nanogpt)is not str or not Path(nanogpt).is_absolute()):raise ValueError()
            if not re.fullmatch(r'[A-Za-z0-9_]{3,80}', value['botUsername']):
                raise ValueError()
            for key in ['tokenFile', 'dbPath']:
                if type(value[key]) is not str or not Path(value[key]).is_absolute():
                    raise ValueError()
            if value['caFile'] is not None and (type(value['caFile']) is not str or not Path(value['caFile']).is_absolute()):
                raise ValueError()
            if type(value['activatedAtMs']) is not int or not 0 <= value['activatedAtMs'] < 2**63:
                raise ValueError()
            return cls(value['enabled'], value['botUsername'], Path(value['tokenFile']),
                       Path(value['caFile']) if value['caFile'] else None, Path(value['dbPath']), value['activatedAtMs'],value.get('platformEnabled',False),Path(nanogpt)if nanogpt else None)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ValueError('invalid_private_max_config') from None
