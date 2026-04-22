from apps.accounts.models import User, AccountStatus


class SessionUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user_id = request.session.get("user_id")
        request.current_user = None

        if user_id:
            user = User.objects.filter(id=user_id).first()
            if user and user.is_active and user.account_status == AccountStatus.ACTIVE:
                request.current_user = user
            else:
                request.session.flush()

        return self.get_response(request)
