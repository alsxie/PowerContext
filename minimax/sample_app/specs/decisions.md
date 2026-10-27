# 设计决策记录

作者：FDE WorkMate

### ADR-001 状态筛选只接受单个状态

- 状态: superseded
- 取代者: ADR-002
- 关联: `OrderQueryService.list_orders`

订单列表的状态筛选参数命名为 `status`，只接受单个状态字符串；传入未知状态时静默返回空列表。

### ADR-002 状态筛选接受状态集合并拒绝未知状态

- 状态: active
- 取代: ADR-001
- 关联: `OrderQueryService.list_orders`, `InvalidQueryError`

客服需要一次查看“已支付和已发货”的订单，因此筛选参数改为状态集合 `statuses`。
未知状态静默返回空列表会掩盖调用方错误，改为抛出 `InvalidQueryError`。
