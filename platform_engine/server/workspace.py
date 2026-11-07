"""Explicit school workspace, kept separate from isolated demonstration fixtures."""
import os

COLLECTIONS = ('schedule', 'teachers', 'students', 'tasks', 'documents',
               'notifications', 'history', 'replacements', 'assignments', 'materials')


def school_workspace() -> bool:
    return os.environ.get('SHKOLA_WORKSPACE_MODE') == 'school'


def empty_school_state() -> dict:
    return {
        'workspaceMode': 'school',
        'school': {'name': 'Идеяфикс', 'students': None, 'classes': None, 'teachers': None},
        **{key: [] for key in COLLECTIONS},
    }


class WorkspaceNeedsCleanup(ValueError):
    pass


def verify_school_state(state: dict) -> None:
    if school_workspace() and state.get('workspaceMode') != 'school':
        raise WorkspaceNeedsCleanup('School workspace requires explicit cleanup of legacy demo data')


def demo_only_path(path: str) -> bool:
    normalized = path.rstrip('/').casefold()
    return any(normalized == root or normalized.startswith(root + '/')
               for root in ('/api/demo', '/api/finance', '/api/assistant'))
