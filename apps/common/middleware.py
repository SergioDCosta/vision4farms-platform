from apps.common.session import resolve_active_session_user


class SessionUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.current_user = None

        user = resolve_active_session_user(request.session)
        if user:
            request.current_user = user
        elif request.session.get("user_id"):
            request.session.flush()

        return self.get_response(request)
