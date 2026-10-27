# 作者：FDE WorkMate
"""内存订单仓储，提供确定性的种子数据。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from orders.models import Order, OrderStatus


class InMemoryOrderRepository:
    """按客户保存订单；仓储只负责存取，不做筛选或排序。"""

    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders: list[Order] = list(orders)

    def add(self, order: Order) -> None:
        self._orders.append(order)

    def by_customer(self, customer_id: str) -> list[Order]:
        return [order for order in self._orders if order.customer_id == customer_id]


def seed_orders() -> list[Order]:
    """生成 c-001 的 6 条订单和 c-002 的 1 条订单，时间与状态固定。"""

    base = datetime(2026, 1, 1, 9, 0, 0)
    statuses = [
        OrderStatus.PENDING,
        OrderStatus.PAID,
        OrderStatus.SHIPPED,
        OrderStatus.PAID,
        OrderStatus.CANCELLED,
        OrderStatus.REFUNDED,
    ]
    orders = [
        Order(
            order_id=f"o-{index + 1:03d}",
            customer_id="c-001",
            status=status,
            amount_cents=1000 * (index + 1),
            created_at=base + timedelta(days=index),
        )
        for index, status in enumerate(statuses)
    ]
    orders.append(Order("o-101", "c-002", OrderStatus.PAID, 4999, base))
    return orders
