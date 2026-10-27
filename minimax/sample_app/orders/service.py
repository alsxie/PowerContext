# 作者：FDE WorkMate
"""订单查询服务。"""

from __future__ import annotations

from orders.errors import InvalidQueryError
from orders.models import Order, Page
from orders.repository import InMemoryOrderRepository

MAX_PAGE_SIZE = 100


class OrderQueryService:
    """面向客户的订单查询入口。"""

    def __init__(self, repository: InMemoryOrderRepository) -> None:
        self._repository = repository

    def list_orders(self, customer_id: str, *, page: int = 1, page_size: int = 20) -> Page:
        """列出客户订单：按创建时间倒序、订单号升序打破平局（REQ-ORD-001），再分页（REQ-ORD-002）。"""

        _validate_pagination(page, page_size)
        orders = self._repository.by_customer(customer_id)
        ordered = sorted(orders, key=_listing_key)
        start = (page - 1) * page_size
        return Page(items=ordered[start : start + page_size], total=len(ordered), page=page, page_size=page_size)

    def total_amount_cents(self, customer_id: str) -> int:
        """客户全部订单金额之和，单位为分（REQ-ORD-003）。"""

        return sum(order.amount_cents for order in self._repository.by_customer(customer_id))


def _listing_key(order: Order) -> tuple[float, str]:
    return (-order.created_at.timestamp(), order.order_id)


def _validate_pagination(page: int, page_size: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise InvalidQueryError("page", "must be an integer >= 1")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise InvalidQueryError("page_size", f"must be an integer between 1 and {MAX_PAGE_SIZE}")

