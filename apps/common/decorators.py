from functools import wraps
from django.shortcuts import redirect


def login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not getattr(request, "current_user", None):
            return redirect("accounts:login")
        return view_func(request, *args, **kwargs)
    return wrapper


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = getattr(request, "current_user", None)

        if not user:
            return redirect("accounts:login")

        if user.role != "ADMIN":
            return redirect("dashboard:painel")

        return view_func(request, *args, **kwargs)
    return wrapper

def client_only_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = getattr(request, "current_user", None)

        if not user:
            return redirect("accounts:login")

        if user.role == "ADMIN":
            return redirect("dashboard:gestor")

        return view_func(request, *args, **kwargs)
    return wrapper