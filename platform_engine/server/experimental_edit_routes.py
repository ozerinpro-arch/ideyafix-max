"""Bound HTTP interface for the versioned school draft editor."""
from contextlib import closing
import sqlite3

from fastapi import APIRouter, Depends, Path as APIPath

from . import db, experimental_edits as edits
from .auth import session_profile
from .experimental_school import SafeExperimentalRoute

router = APIRouter(prefix='/experimental-school', tags=['experimental edits'], route_class=SafeExperimentalRoute)
ID_PATTERN = r'^exp_[a-f0-9]{32}$'


@router.get('/{experiment_id}/version')
def version(experiment_id: str = APIPath(pattern=ID_PATTERN), profile=Depends(session_profile)):
    from . import experimental_publications as publications
    # get_version verifies school membership and the exact saved source. Read the
    # publication pointer in the same read transaction as the editor version.
    edits._actor(profile)
    path = db.database_path()
    if not path.is_file():
        return edits.get_version(experiment_id, profile)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)) as connection:
        connection.execute('BEGIN')
        value = edits.read_version(connection, experiment_id, profile)
        value['publication'] = publications.publication_state(connection, experiment_id, profile)
        value['actor'] = {'userId': profile['userId'], 'membershipId': profile.get('membershipId'), 'role': profile.get('staffRole', profile['role'])}
        # Validator internals and repeated source models stay on the server.
        value = {key: item for key, item in value.items()
                 if not key.startswith('_') and key not in {'originalInput', 'assignments', 'eventIdMap', 'originalProvenance'}}
        value['catalog'] = {key: value['input'][key] for key in ('teachers', 'groups', 'rooms', 'slots')}
        del value['input']
        return value


@router.post('/{experiment_id}/edits/preview')
def preview(body: edits.ExperimentalEditInput, experiment_id: str = APIPath(pattern=ID_PATTERN), profile=Depends(session_profile)):
    return edits.preview_edit(experiment_id, body, profile)


@router.post('/{experiment_id}/edits/apply')
def apply(body: edits.ExperimentalEditInput, experiment_id: str = APIPath(pattern=ID_PATTERN), profile=Depends(session_profile)):
    return edits.apply_edit(experiment_id, body, profile)

# The cancellation ledger shares the editor's school/role boundaries.
from .lesson_makeups import router as makeup_router
router.include_router(makeup_router)
