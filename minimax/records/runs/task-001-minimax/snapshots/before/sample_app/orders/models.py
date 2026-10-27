# 作者：FDE WorkMate
"""订单领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderStatus(str, Enum):
    """订单状态，取值见 specs/orders.md 的 REQ-ORD-003。"""

    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


@dataclass(frozen=True)
class Order:
    """一条订单记录，金额以整数分保存（REQ-ORD-003）。"""

    order_id: str
    customer_id: str
    status: OrderStatus
    amount_cents: int
    created_at: datetime


@dataclass(frozen=True)
class Page:
    """分页查询结果；total 为分页前满足条件的记录总数。"""

    items: list[Order] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
