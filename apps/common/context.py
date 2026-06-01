EMPTY_BADGE = {"visible": False, "count": 0, "tone": "orange"}


def empty_badge():
    return dict(EMPTY_BADGE)


def request_cache(request):
    cache = getattr(request, "_common_context_cache", None)
    if cache is None:
        cache = {}
        setattr(request, "_common_context_cache", cache)
    return cache


def cached(request, key, factory):
    cache = request_cache(request)
    if key not in cache:
        cache[key] = factory()
    return cache[key]

