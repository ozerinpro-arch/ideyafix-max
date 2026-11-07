"""Explicit private NanoGPT configuration; activation remains fail-closed."""
import json
import os
from pathlib import Path
import re
import stat

ENDPOINT = 'https://nano-gpt.com/api/v1'


def _pairs(items):
    value={}
    for key,item in items:
        if key in value:raise ValueError('duplicate_config_key')
        value[key]=item
    return value


def _private(path, maximum):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in [path,*path.parents]):
        raise ValueError('invalid_nanogpt_config')
    info=path.stat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
            or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_size>maximum):
        raise ValueError('invalid_nanogpt_config')
    return path


def load_config(filename):
    """Validate disabled settings without opening or reading the API key file.

    No usable free API model has been authorized. Enabling requests requires a
    separately approved provider/budget implementation; this loader cannot spend.
    """
    try:
        path=_private(filename,4096)
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as stream:
            raw=stream.read(4097)
        if len(raw)>4096:raise ValueError()
        value=json.loads(raw,object_pairs_hook=_pairs)
        if (type(value)is not dict or set(value)!={'apiKeyFile','model','requestsEnabled'}
                or type(value['requestsEnabled'])is not bool
                or not isinstance(value['model'],str)
                or value['model'] and not re.fullmatch(r'[A-Za-z0-9_./:+-]{1,120}',value['model'])
                or not isinstance(value['apiKeyFile'],str)):
            raise ValueError()
        key_path=_private(value['apiKeyFile'],8192)
        project=Path(__file__).resolve().parent.parent
        if key_path.parent!=path.parent and not key_path.is_relative_to(project/'runtime'):
            raise ValueError()
        if value['requestsEnabled']:
            raise ValueError('nanogpt_requests_not_authorized')
        return {'endpoint':ENDPOINT,'model':value['model'],'requestsEnabled':False}
    except (OSError,ValueError,TypeError,UnicodeError):
        raise ValueError('invalid_or_unauthorized_nanogpt_config') from None
