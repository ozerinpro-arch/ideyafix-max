"""Read-only polling heartbeat; never reads a bot token or contacts a provider."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.max_settings import MaxSettings


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    try:
        settings=MaxSettings.load(args.config)
        if not settings.enabled:
            print(json.dumps({'status':'disabled'}));return 0
        path=settings.db_path
        if not path.is_file()or any(p.is_symlink()for p in [path,*path.parents]):
            raise ValueError('heartbeat_unavailable')
        info=path.stat();parent=path.parent.stat()
        if (info.st_uid!=os.getuid()or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600
                or parent.st_uid!=os.getuid()or stat.S_IMODE(parent.st_mode)!=0o700):
            raise ValueError('private_heartbeat_required')
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)as database:
            row=database.execute("SELECT value FROM live_meta WHERE key='heartbeat'").fetchone()
        age=time.time()-float(row[0])if row else float('inf')
        if not 0<=age<40:raise ValueError('heartbeat_stale')
        print(json.dumps({'status':'ok'}));return 0
    except Exception:
        print(json.dumps({'status':'unavailable'}));return 1


if __name__=='__main__':
    raise SystemExit(main())
