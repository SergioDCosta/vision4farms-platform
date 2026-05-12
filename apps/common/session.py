from apps.accounts.models import AccountStatus, User
from apps.accounts.services import is_valid_session_auth_fingerprint


def resolve_active_session_user(session):
    if not session:
        return None

    user_id = session.get("user_id")
    if not user_id:
        return None

    user = User.objects.filter(id=user_id).first()
    if not user or not user.is_active or user.account_status != AccountStatus.ACTIVE:
        return None

    session_auth_fingerprint = session.get("session_auth_fingerprint")
    if not is_valid_session_auth_fingerprint(user, session_auth_fingerprint):
        return None

    return user
