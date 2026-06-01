from apps.inventory.models import ProducerProfile


def get_current_producer_for_user(user):
    if not user:
        return None
    return ProducerProfile.objects.filter(user=user).first()

