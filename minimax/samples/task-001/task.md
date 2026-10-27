# TASK-001 订单列表按状态筛选

作者：FDE WorkMate

## 要改什么

客服需要在订单列表里只看指定状态的订单。为 `OrderQueryService.list_orders` 增加可选关键字参数 `statuses`，
按状态集合筛选订单，对空集合或未知状态抛出 `InvalidQueryError`（参数名 statuses），
并保证筛选在排序和分页之前生效。

## 哪些原有行为必须保持

- 不传 statuses 时，列表结果与修改前完全一致。
- 排序规则不变：created_at 倒序，同一时间按 order_id 升序。
- 分页参数校验与错误参数名不变；total 为筛选后、分页前的总数。
- 客户消费总额 `total_amount_cents` 不受影响。

## 验证

- 目标功能测试：`sample_app/tests/test_status_filter.py`
- 回归测试：`sample_app/tests/test_listing.py`、`sample_app/tests/test_billing.py`
