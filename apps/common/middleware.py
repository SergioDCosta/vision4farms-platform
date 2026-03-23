from apps.accounts.models import User


class SessionUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user_id = request.session.get("user_id")

        if user_id:
            request.current_user = User.objects.filter(id=user_id).first()
        else:
            request.current_user = None

        return self.get_response(request)