from urllib.parse import urlencode

from django.urls import reverse


def build_needs_index_url(
    *,
    q="",
    category_id="",
    selected_need_id="",
    need_prefill_product_id="",
    need_prefill_quantity="",
    show_need_form=False,
):
    query = {}
    if q:
        query["q"] = q
    if category_id:
        query["category"] = category_id
    if selected_need_id:
        query["need"] = selected_need_id
    if need_prefill_product_id:
        query["product"] = need_prefill_product_id
    if need_prefill_quantity:
        query["qty"] = need_prefill_quantity
    if show_need_form:
        query["show_need_form"] = "1"
    url = reverse("needs:index")
    return f"{url}?{urlencode(query)}" if query else url
