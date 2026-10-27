# 开发上下文：TASK-001 订单列表按状态筛选

资料库：seekdb，导入批次 d133337fb3e44aa699f9c6566bb93aac，导入时间 2026-10-27T16:03:11+00:00

## 任务

要改什么：
客服需要在订单列表里只看指定状态的订单。为 `OrderQueryService.list_orders` 增加可选关键字参数 `statuses`，
按状态集合筛选订单，对空集合或未知状态抛出 `InvalidQueryError`（参数名 statuses），
并保证筛选在排序和分页之前生效。

必须保持：
- 不传 statuses 时，列表结果与修改前完全一致。
- 排序规则不变：created_at 倒序，同一时间按 order_id 升序。
- 分页参数校验与错误参数名不变；total 为筛选后、分页前的总数。
- 客户消费总额 `total_amount_cents` 不受影响。

## 适用规格

### REQ-ORD-004 按状态筛选订单列表（primary，来源 specs/orders.md:L30-L43，hash c9d08eb5c54e2144）

订单列表新增可选关键字参数 `statuses`，用于按订单状态筛选：

1. 不传或传 `None` 时不筛选，行为与原有列表完全一致。
2. 传入状态集合（list、tuple 或 set，元素为 `OrderStatus` 或其字符串值）时，只返回状态属于该集合的订单。
3. 集合为空，或包含 REQ-ORD-003 之外的状态值时，抛出 `InvalidQueryError`，参数名为 `statuses`。
4. 筛选在排序和分页之前进行：`total` 为筛选后的总数，排序继续遵守 REQ-ORD-001。
5. 状态集合中重复出现的值只计一次，每条订单在结果中最多出现一次。
6. 字符串状态值区分大小写，`"PAID"` 不是合法状态，按第 3 条处理。

### ADR-002 状态筛选接受状态集合并拒绝未知状态（constraint，来源 specs/decisions.md:L13-L20，hash 9b01cd0c2876b498）

客服需要一次查看“已支付和已发货”的订单，因此筛选参数改为状态集合 `statuses`。
未知状态静默返回空列表会掩盖调用方错误，改为抛出 `InvalidQueryError`。

### REQ-BIL-001 客户消费总额（constraint，来源 specs/billing.md:L5-L10，hash 08f66f8cc576080e）

客户消费总额为该客户全部订单 `amount_cents` 之和，单位为分，不做四舍五入。

### REQ-ORD-001 订单列表排序（constraint，来源 specs/orders.md:L7-L13，hash c4ac01a50a6cc141）

客户订单列表按 `created_at` 倒序返回；创建时间相同时按 `order_id` 升序排列，保证结果稳定。
任何筛选条件都不得改变这一排序规则。

### REQ-ORD-002 订单列表分页（constraint，来源 specs/orders.md:L15-L21，hash 6afc6f419a3678a9）

`page` 从 1 开始，`page_size` 取值 1 到 100，超出范围时抛出 `InvalidQueryError`，错误信息必须指出出错的参数名。
返回的 `total` 是分页前、所有筛选条件生效后的记录总数。

### REQ-ORD-003 订单状态与金额（constraint，来源 specs/orders.md:L23-L28，hash 5b3bf4f603dfd180）

订单状态只允许 pending、paid、shipped、cancelled、refunded 五种取值。金额以整数分保存。

## 已失效的决策（不得遵循）

- ADR-001 状态筛选只接受单个状态（来源 specs/decisions.md:L5-L11，被 ADR-002 取代）

## 需要修改的代码

文件：orders/service.py

### OrderQueryService.list_orders（orders/service.py:L19-L42）

```python
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
```

## 参考代码（不需要修改，除非规格要求）

### InvalidQueryError（orders/errors.py:L5-L10，主规格 REQ-ORD-004 关联的类型/模块）

```python
class InvalidQueryError(ValueError):
    """查询参数不合法时抛出，message 必须指出出错的参数（REQ-ORD-002）。"""

    def __init__(self, parameter: str, message: str) -> None:
        super().__init__(f"{parameter}: {message}")
        self.parameter = parameter
```

### OrderStatus（orders/models.py:L11-L18，主规格 REQ-ORD-004 关联的类型/模块）

```python
class OrderStatus(str, Enum):
    """订单状态，取值见 specs/orders.md 的 REQ-ORD-003。"""

    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
```

### Page（orders/models.py:L32-L39，被待修改符号调用）

```python
@dataclass(frozen=True)
class Page:
    """分页查询结果；total 为分页前满足条件的记录总数。"""

    items: list[Order] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
```

### _listing_key（orders/service.py:L84-L85，被待修改符号调用）

```python
def _listing_key(order: Order) -> tuple[float, str]:
    return (-order.created_at.timestamp(), order.order_id)
```

### _status_value（orders/service.py:L50-L51，被待修改符号调用）

```python
def _status_value(status: OrderStatus | str) -> str:
    return status.value if isinstance(status, OrderStatus) else status
```

### _validate_pagination（orders/service.py:L88-L92，被待修改符号调用）

```python
def _validate_pagination(page: int, page_size: int) -> None:
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise InvalidQueryError("page", "must be an integer >= 1")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise InvalidQueryError("page_size", f"must be an integer between 1 and {MAX_PAGE_SIZE}")
```

### _validate_statuses（orders/service.py:L54-L81，被待修改符号调用）

```python
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
```

## 测试

目标功能测试（修改后必须全部通过）：
- tests/test_status_filter.py::test_filter_accepts_string_values（tests/test_status_filter.py:L25-L27）
- tests/test_status_filter.py::test_filter_applies_before_pagination（tests/test_status_filter.py:L30-L33）
- tests/test_status_filter.py::test_filter_by_status_set_keeps_sorting（tests/test_status_filter.py:L19-L22）
- tests/test_status_filter.py::test_invalid_statuses_raise_with_parameter_name（tests/test_status_filter.py:L40-L44）
- tests/test_status_filter.py::test_none_statuses_keeps_original_listing（tests/test_status_filter.py:L36-L37）

回归测试（原有行为，必须保持通过）：
- tests/test_billing.py::test_total_amount_sums_all_orders（tests/test_billing.py:L8-L11，引用约束规格 REQ-BIL-001）
- tests/test_listing.py::test_invalid_pagination_names_parameter（tests/test_listing.py:L57-L61，调用待修改符号 list_orders）
- tests/test_listing.py::test_list_orders_breaks_ties_by_order_id（tests/test_listing.py:L26-L35，调用待修改符号 list_orders）
- tests/test_listing.py::test_list_orders_only_returns_customer_orders（tests/test_listing.py:L38-L41，调用待修改符号 list_orders）
- tests/test_listing.py::test_list_orders_sorted_by_created_at_desc（tests/test_listing.py:L21-L23，调用待修改符号 list_orders）
- tests/test_listing.py::test_pagination_slices_after_sorting（tests/test_listing.py:L44-L48，调用待修改符号 list_orders）
- tests/test_listing.py::test_unknown_customer_returns_empty_page（tests/test_listing.py:L51-L54，调用待修改符号 list_orders）

目标功能测试代码：

```python
def test_filter_accepts_string_values(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", statuses=["refunded", "cancelled"])
    assert [order.order_id for order in page.items] == ["o-006", "o-005"]
```

```python
def test_filter_applies_before_pagination(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", statuses=("paid", "shipped", "pending"), page=2, page_size=3)
    assert [order.order_id for order in page.items] == ["o-001"]
    assert page.total == 4
```

```python
def test_filter_by_status_set_keeps_sorting(service: OrderQueryService) -> None:
    page = service.list_orders("c-001", statuses={OrderStatus.PAID, OrderStatus.SHIPPED})
    assert [order.order_id for order in page.items] == ["o-004", "o-003", "o-002"]
    assert page.total == 3
```

```python
@pytest.mark.parametrize("statuses", [[], ["paid", "lost"], ["PAID-X"]])
def test_invalid_statuses_raise_with_parameter_name(service: OrderQueryService, statuses: list[str]) -> None:
    with pytest.raises(InvalidQueryError) as error:
        service.list_orders("c-001", statuses=statuses)
    assert error.value.parameter == "statuses"
```

```python
def test_none_statuses_keeps_original_listing(service: OrderQueryService) -> None:
    assert service.list_orders("c-001", statuses=None) == service.list_orders("c-001")
```

## 任务状态（PowerContext）

Scope：scp_2qdjtjt166e4t6f60fdzynf232，Handoff 版本：6

已完成：
- [ingest] 导入规格、代码和测试到 seekdb：导入 43 条（规格 8，代码 23，测试 12），内容变化 6 条，仅位置变化 3 条
- [baseline] 修改前运行目标功能测试，确认因功能未实现而失败：目标功能测试 7/7 因功能未实现失败（exit 1），回归测试 9/9 通过（exit 0）
- [context] 从 seekdb 检索并生成开发上下文：主规格 REQ-ORD-004，适用规格 6 条，待修改符号 OrderQueryService.list_orders，功能测试函数 5 个，回归测试函数 7 个
- [agent] MiniMax 根据上下文完成代码修改：MiniMax（live，MiniMax-M3.1-Flash）第 3 次修改 orders/service.py
- [verify] 修改后运行目标功能测试和回归测试，全部通过：目标功能测试 7/7 通过，回归测试 9/9 通过，全量 16/16 通过（exit 0/0/0）

待办：
- （无）

已有验证结果：
- [baseline] 目标功能测试 7/7 因功能未实现失败（exit 1），回归测试 9/9 通过（exit 0）（规格或代码已变化，需重新验证）
- [verify] 目标功能测试 7/7 通过，回归测试 9/9 通过，全量 16/16 通过（exit 0/0/0）（规格或代码已变化，需重新验证）

上次生成上下文后发生变化的规格：
- REQ-ORD-004：8be251206d0eb974 → c9d08eb5c54e2144

