from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.urls import reverse

from apps.needs.models import NeedSourceSystem, NeedStatus
from apps.needs.services import (
    calculate_need_coverage,
    create_or_update_need,
    ignore_need,
    list_marketplace_public_needs,
)
from apps.orders.models import OrderItemStatus, OrderStatus


class NeedsRoutingTests(SimpleTestCase):
    def test_needs_index_url_is_public_needs_path(self):
        self.assertEqual(reverse("needs:index"), "/necessidades/")


class NeedsServiceTests(SimpleTestCase):
    def test_calculate_need_coverage_counts_planned_and_completed_items(self):
        need = SimpleNamespace(id="need-1", required_quantity=Decimal("10"))
        items = [
            SimpleNamespace(
                item_status=OrderItemStatus.COMPLETED,
                quantity=Decimal("2"),
                order=SimpleNamespace(status=OrderStatus.COMPLETED),
            ),
            SimpleNamespace(
                item_status=OrderItemStatus.CONFIRMED,
                quantity=Decimal("3"),
                order=SimpleNamespace(status=OrderStatus.CONFIRMED),
            ),
            SimpleNamespace(
                item_status=OrderItemStatus.CANCELLED,
                quantity=Decimal("4"),
                order=SimpleNamespace(status=OrderStatus.CANCELLED),
            ),
        ]

        with patch(
            "apps.needs.services.OrderItem.objects.filter"
        ) as filter_items:
            filter_items.return_value.select_related.return_value = items
            coverage = calculate_need_coverage(need)

        self.assertEqual(coverage["required_quantity"], Decimal("10.000"))
        self.assertEqual(coverage["planned_qty"], Decimal("5.000"))
        self.assertEqual(coverage["completed_qty"], Decimal("2.000"))
        self.assertEqual(coverage["remaining_to_plan"], Decimal("5.000"))

    def test_create_or_update_need_updates_existing_active_need(self):
        producer = SimpleNamespace(id="producer-1")
        product = SimpleNamespace(id="product-1")
        need = MagicMock(
            producer=producer,
            product=product,
            status=NeedStatus.OPEN,
        )
        need.updated_at = None
        active_qs = MagicMock()
        active_qs.order_by.return_value = [need]
        manager = MagicMock()
        manager.objects.select_for_update.return_value.filter.return_value = active_qs
        create_or_update = getattr(create_or_update_need, "__wrapped__", create_or_update_need)

        with (
            patch("apps.needs.services.Need", manager),
            patch(
                "apps.needs.services.recalculate_need_status",
                return_value=(need, {"remaining_to_plan": Decimal("4.000")}, False),
            ),
        ):
            result, _, created = create_or_update(
                producer=producer,
                product=product,
                required_quantity=Decimal("7"),
                source_system=NeedSourceSystem.MANUAL,
                notes="Observação",
            )

        self.assertIs(result, need)
        self.assertFalse(created)
        self.assertEqual(need.required_quantity, Decimal("7.000"))
        self.assertEqual(need.notes, "Observação")

    def test_ignore_need_marks_need_as_ignored(self):
        producer = SimpleNamespace(id="producer-1")
        need = MagicMock(producer_id="producer-1", status=NeedStatus.OPEN)
        ignore = getattr(ignore_need, "__wrapped__", ignore_need)

        changed = ignore(need=need, producer=producer)

        self.assertTrue(changed)
        self.assertEqual(need.status, NeedStatus.IGNORED)
        need.save.assert_called()

    def test_public_needs_hide_partially_covered_status(self):
        need = SimpleNamespace(
            id="need-1",
            status=NeedStatus.PARTIALLY_COVERED,
            producer=SimpleNamespace(display_name="Produtor A"),
            required_quantity=Decimal("10"),
            get_status_display=lambda: "Parcialmente Coberta",
        )
        qs = MagicMock()
        qs.exclude.return_value = qs
        qs.filter.return_value = qs
        qs.order_by.return_value = qs
        manager = MagicMock()
        manager.objects.select_related.return_value.filter.return_value = qs
        qs.__iter__.return_value = iter([need])

        with (
            patch("apps.needs.services.Need", manager),
            patch(
                "apps.needs.services.calculate_need_coverage",
                return_value={
                    "required_quantity": Decimal("10.000"),
                    "planned_qty": Decimal("4.000"),
                    "completed_qty": Decimal("0.000"),
                    "remaining_to_plan": Decimal("6.000"),
                    "remaining_to_receive": Decimal("10.000"),
                },
            ),
        ):
            rows = list_marketplace_public_needs(viewer_producer=SimpleNamespace(id="viewer-1"))

        self.assertEqual(rows[0]["public_status"], NeedStatus.OPEN)
        self.assertEqual(rows[0]["public_status_label"], "Aberta")
        self.assertEqual(rows[0]["public_quantity"], Decimal("6.000"))
