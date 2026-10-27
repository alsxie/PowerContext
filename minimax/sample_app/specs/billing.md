# 计费规格

作者：FDE WorkMate

### REQ-BIL-001 客户消费总额

- 状态: active
- 关联: `OrderQueryService.total_amount_cents`

客户消费总额为该客户全部订单 `amount_cents` 之和，单位为分，不做四舍五入。

### REQ-BIL-002 发票抬头

- 状态: active

发票抬头由财务系统维护，本服务不保存发票信息。
