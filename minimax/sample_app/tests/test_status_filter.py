# 作者：FDE WorkMate
"""TASK-001 目标功能测试：按状态筛选订单列表（REQ-ORD-004、ADR-002）。"""

from __future__ import annotations

import pytest

from orders.errors import InvalidQueryError
from orders.models import OrderStatus
from orders.repository import InMemoryOrderRepository, seed_orders
from orders.service import OrderQueryService


@pytest.fixture
def service() -> OrderQueryService:
    return OrderQueryService(InMemoryOrderRepository(seed_orders()))


def test_filter_by_status_set_keeps_sorting(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", statuses={OrderStatus.PAID, OrderStatus.SHIPPED})
    assert [order.order_id for order in page.items] == ["o-004", "o-003", "o-002"]
    assert page.total == 3


def test_filter_accepts_string_values(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", statuses=["refunded", "cancelled"])
    assert [order.order_id for order in page.items] == ["o-006", "o-005"]


def test_filter_applies_before_pagination(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", statuses=("paid", "shipped", "pending"), page=2, page_size=3)
    assert [order.order_id for order in page.items] == ["o-001"]
    assert page.total == 4


def test_none_statuses_keeps_original_listing(service: OrderQueryService) -> None:
    assert service.list_orders("c-001", statuses=None) == service.list_orders("c-001")


@pytest.mark.parametrize("statuses", [[], ["paid", "lost"], ["PAID-X"]])
def test_invalid_statuses_raise_with_parameter_name(service: OrderQueryService, statuses: list[str]) -> None:
    with pytest.raises(InvalidQueryError) as error:
        service.list_orders("c-001", statuses=statuses)
    assert error.value.parameter == "statuses"
