# 作者：FDE WorkMate
"""订单列表回归测试：排序、分页与参数校验（REQ-ORD-001、REQ-ORD-002）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from orders.errors import InvalidQueryError
from orders.models import Order, OrderStatus
from orders.repository import InMemoryOrderRepository, seed_orders
from orders.service import OrderQueryService


@pytest.fixture
def service() -> OrderQueryService:
    return OrderQueryService(InMemoryOrderRepository(seed_orders()))


def test_list_orders_sorted_by_created_at_desc(service: OrderQueryService) -> None:
    page = service.list_orders("c-001")
    assert [order.order_id for order in page.items] == ["o-006", "o-005", "o-004", "o-003", "o-002", "o-001"]


def test_list_orders_breaks_ties_by_order_id() -> None:
    same_time = datetime(2026, 2, 1)
    repository = InMemoryOrderRepository(
        [
            Order("o-b", "c-9", OrderStatus.PAID, 1, same_time),
            Order("o-a", "c-9", OrderStatus.PAID, 1, same_time),
        ]
    )
    page = OrderQueryService(repository).list_orders("c-9")
    assert [order.order_id for order in page.items] == ["o-a", "o-b"]


def test_list_orders_only_returns_customer_orders(service: OrderQueryService) -> None:
    page = service.list_orders("c-002")
    assert [order.order_id for order in page.items] == ["o-101"]
    assert page.total == 1


def test_pagination_slices_after_sorting(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", page=2, page_size=4)
    assert [order.order_id for order in page.items] == ["o-002", "o-001"]
    assert page.total == 6
    assert (page.page, page.page_size) == (2, 4)


def test_unknown_customer_returns_empty_page(service: OrderQueryService) -> None:
    page = service.list_orders("c-404")
    assert page.items == []
    assert page.total == 0


@pytest.mark.parametrize(("page", "page_size", "parameter"), [(0, 20, "page"), (1, 0, "page_size"), (1, 101, "page_size")])
def test_invalid_pagination_names_parameter(service: OrderQueryService, page: int, page_size: int, parameter: str) -> None:
    with pytest.raises(InvalidQueryError) as error:
        service.list_orders("c-001", page=page, page_size=page_size)
    assert error.value.parameter == parameter
