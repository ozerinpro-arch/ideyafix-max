"""Host-only identity wiring for offline workflows; never an HTTP identity API."""
from copy import deepcopy
from threading import RLock

_portal_principals = {}
_lock = RLock()


def register_portal_principal(profile):
    """Only the closed portal factory calls this with its fixed principal.

    Existing gateway authentication remains the boundary for incoming requests.
    This record permits offline preparation; no live delivery is enabled.
    """
    if profile.get('identitySource') not in {'portal-owner', 'portal-preview'}:
        raise ValueError('Expected fixed portal principal')
    key = (profile['schoolId'], profile['userId'])
    with _lock:
        _portal_principals[key] = deepcopy(profile)


def membership_status(profile):
    """Authoritative mismatch differs from this process lacking a host registry.

    No registration is learned from a persisted task or client identity. A peer
    process can deny execution without declaring another host principal revoked.
    """
    if not isinstance(profile, dict) or profile.get('membershipVerified') is not True:
        return 'invalid'
    if profile.get('staffRole'):
        from . import staff_access
        status = getattr(staff_access, 'membership_status', None)
        if status is not None:
            return status(profile)
        # Old staff hosts do not distinguish revoked membership from a missing
        # registry/unreadable authority. Never turn that ambiguity into stale.
        return 'current' if staff_access.membership_current(profile) else 'unverifiable'
    with _lock:
        expected = _portal_principals.get((profile.get('schoolId'), profile.get('userId')))
    if expected is None:
        return 'unverifiable'
    return 'current' if all(profile.get(k) == expected.get(k) for k in
        ('identitySource', 'schoolId', 'userId', 'membershipId', 'membershipRevision', 'role', 'permissions')) else 'invalid'


def membership_current(profile):
    return membership_status(profile) == 'current'


def resolve_membership(school_id, membership_id):
    from .staff_access import resolve_membership as resolve_staff
    try:
        result = resolve_staff(school_id, membership_id)
    except (RuntimeError, OSError):
        result = None
    if result is not None:
        return result
    with _lock:
        result = next((deepcopy(p) for p in _portal_principals.values()
                       if p['schoolId'] == school_id and p['membershipId'] == membership_id), None)
    return result


def list_respondents(school_id):
    from .staff_access import list_respondents as list_staff
    result = list_staff(school_id)
    with _lock:
        result.extend(deepcopy(p) for p in _portal_principals.values() if p['schoolId'] == school_id)
    return [{'membershipId': p['membershipId'], 'userId': p['userId'],
             'name': p.get('name', 'Уполномоченный сотрудник'),
             'staffRole': p.get('staffRole', p.get('role'))}
            for p in result if membership_current(p) and p.get('staffRole', p.get('role')) in
            {'director', 'planner', 'teacher'}]


def install():
    from .launch_runtime import install_membership_verifier
    install_membership_verifier(membership_current, resolve_membership, list_respondents, membership_status)
