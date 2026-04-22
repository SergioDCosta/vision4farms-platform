from functools import wraps
from django.shortcuts import redirect
from apps.accounts.models import AccountStatus


def _get_active_session_user(request):
    user = getattr(request, "current_user", None)
    if user and user.is_active and user.account_status == AccountStatus.ACTIVE:
        return user

    if request.session.get("user_id"):
        request.session.flush()

    return None


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _get_active_session_user(request):
            return redirect("accounts:login")
        return view_func(request, *args, **kwargs)
    return wrapper


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = _get_active_session_user(request)
        if not user:
            return redirect("accounts:login")

        if user.role != "ADMIN":
            return redirect("dashboard:painel")

        return view_func(request, *args, **kwargs)
    return wrapper

def client_only_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = _get_active_session_user(request)
        if not user:
            return redirect("accounts:login")

        if user.role == "ADMIN":
            return redirect("dashboard:gestor")

        return view_func(request, *args, **kwargs)
    return wrapper
