# 作者：FDE WorkMate
"""计费回归测试（REQ-BIL-001）。"""

from orders.repository import InMemoryOrderRepository, seed_orders
from orders.service import OrderQueryService


def test_total_amount_sums_all_orders() -> None:
    service = OrderQueryService(InMemoryOrderRepository(seed_orders()))
    assert service.total_amount_cents("c-001") == 21000
    assert service.total_amount_cents("c-002") == 4999
