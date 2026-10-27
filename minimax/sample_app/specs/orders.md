# 订单查询规格

作者：FDE WorkMate

本文件中每个 `### <ID>` 标题是一条可独立引用的规格条目。`状态` 取值为 active 或 superseded。

### REQ-ORD-001 订单列表排序

- 状态: active
- 关联: `OrderQueryService.list_orders`, `orders/service.py`

客户订单列表按 `created_at` 倒序返回；创建时间相同时按 `order_id` 升序排列，保证结果稳定。
任何筛选条件都不得改变这一排序规则。

### REQ-ORD-002 订单列表分页

- 状态: active
- 关联: `OrderQueryService.list_orders`, `InvalidQueryError`

`page` 从 1 开始，`page_size` 取值 1 到 100，超出范围时抛出 `InvalidQueryError`，错误信息必须指出出错的参数名。
返回的 `total` 是分页前、所有筛选条件生效后的记录总数。

### REQ-ORD-003 订单状态与金额

- 状态: active
- 关联: `OrderStatus`, `Order`

订单状态只允许 pending、paid、shipped、cancelled、refunded 五种取值。金额以整数分保存。

### REQ-ORD-004 按状态筛选订单列表

- 状态: active
- 关联: `OrderQueryService.list_orders`, `OrderStatus`, `InvalidQueryError`
- 依据: ADR-002

订单列表新增可选关键字参数 `statuses`，用于按订单状态筛选：

1. 不传或传 `None` 时不筛选，行为与原有列表完全一致。
2. 传入状态集合（list、tuple 或 set，元素为 `OrderStatus` 或其字符串值）时，只返回状态属于该集合的订单。
3. 集合为空，或包含 REQ-ORD-003 之外的状态值时，抛出 `InvalidQueryError`，参数名为 `statuses`。
4. 筛选在排序和分页之前进行：`total` 为筛选后的总数，排序继续遵守 REQ-ORD-001。
