from decimal import Decimal, InvalidOperation

from django.utils import formats


def format_quantity(value, max_places=3):
    try:
        max_places = int(max_places)
    except (TypeError, ValueError):
        max_places = 3
    max_places = max(0, max_places)

    if value in (None, ""):
        value = 0

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value

    quantizer = Decimal("1") if max_places == 0 else Decimal("1").scaleb(-max_places)
    decimal_value = decimal_value.quantize(quantizer)

    if decimal_value == 0:
        decimal_places = 0
    else:
        normalized = decimal_value.normalize()
        decimal_places = max(0, -normalized.as_tuple().exponent)
        decimal_places = min(decimal_places, max_places)

    return formats.number_format(
        decimal_value,
        decimal_pos=decimal_places,
        use_l10n=True,
        force_grouping=False,
    )
