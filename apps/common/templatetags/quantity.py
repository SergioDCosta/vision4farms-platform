from django import template

from apps.common.formatting import format_quantity


register = template.Library()


@register.filter(name="quantity")
def quantity(value, max_places=3):
    return format_quantity(value, max_places=max_places)
