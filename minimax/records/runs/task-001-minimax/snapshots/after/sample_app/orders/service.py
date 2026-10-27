# 作者：FDE WorkMate
"""订单查询服务。"""

from __future__ import annotations

from orders.errors import InvalidQueryError
from orders.models import Order, OrderStatus, Page
from orders.repository import InMemoryOrderRepository

MAX_PAGE_SIZE = 100


class OrderQueryService:
    """面向客户的订单查询入口。"""

    def __init__(self, repository: InMemoryOrderRepository) -> None:
        self._repository = repository

    def list_orders(
        self,
        customer_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        statuses: list[OrderStatus | str] | tuple[OrderStatus | str, ...] | set[OrderStatus | str] | None = None,
    ) -> Page:
        """列出客户订单：可按状态筛选，再按创建时间倒序、订单号升序排序并分页。"""

        _validate_pagination(page, page_size)
        status_values = _validate_statuses(statuses)

        orders = self._repository.by_customer(customer_id)
        if status_values is not None:
            orders = [
                order
                for order in orders
                if _status_value(order.status) in status_values
            ]

        ordered = sorted(orders, key=_listing_key)
        start = (page - 1) * page_size
        return Page(items=ordered[start : start + page_size], total=len(ordered), page=page, page_size=page_size)

    def total_amount_cents(self, customer_id: str) -> int:
        """客户全部订单金额之和，单位为分（REQ-ORD-003）。"""

        return sum(order.amount_cents for order in self._repository.by_customer(customer_id))


def _status_value(status: OrderStatus | str) -> str:
    return status.value if isinstance(status, OrderStatus) else status


def _validate_statuses(
    statuses: list[OrderStatus | str] | tuple[OrderStatus | str, ...] | set[OrderStatus | str] | None,
) -> set[str] | None:
    if statuses is None:
        return None

    if not isinstance(statuses, (list, tuple, set, frozenset)):
        raise InvalidQueryError("statuses", "must be a list, tuple, or set of valid order statuses")

    if not statuses:
        raise InvalidQueryError("statuses", "must not be empty")

    valid_statuses = {status.value for status in OrderStatus}
    normalized: set[str] = set()

    for status in statuses:
        if isinstance(status, OrderStatus):
            value = status.value
        elif isinstance(status, str):
            value = status
        else:
            raise InvalidQueryError("statuses", "must contain only valid order statuses")

        if value not in valid_statuses:
            raise InvalidQueryError("statuses", "must contain only valid order statuses")
        normalized.add(value)

    return normalized


def _listing_key(order: Order) -> tuple[float, str]:
    return (-order.created_at.timestamp(), order.order_id)


def _validate_pagination(page: int, page_size: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise InvalidQueryError("page", "must be an integer >= 1")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise InvalidQueryError("page_size", f"must be an integer between 1 and {MAX_PAGE_SIZE}")
