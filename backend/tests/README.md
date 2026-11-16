# 后端测试

不依赖 MySQL/Redis，使用 SQLite + FastAPI TestClient + 模拟 worker 计算即可运行。

```sh
# 仅需 fastapi / sqlalchemy / pydantic / pyjwt / httpx / redis-py
python3 tests/test_recalc_engine.py   # 撤回重算纯函数：剔除、对账、规则消失、重叠撤回、样本不足
python3 tests/test_integration.py     # 端到端：受理/隔离/重算/失效/门控/幂等/并发/宕机恢复（41 项断言）
```

覆盖的验收场景：

- 受理撤回 → 影响追踪（受影响批次与备件包）→ 批次隔离 → 冻结样本剔除重算 → 前后计数对账
- 规则消失置 invalid 且来源备件包失效；仍成立规则保留原 rule_id 并更新证据；无关批次不变
- 新挖掘样本排除撤回作业；隔离批次禁止导出/起草/签核，纠正完成后放行
- 重试同一撤回不重复扣减；纠正 pending→completed，processing 中断后可恢复
- 重叠撤回（作业交集）累积扣减、并发受理串行化且只生成一张撤回单
- 重算中宕机：processing 纠正恢复后继续，最终一致

> 注：SQLite 无 `GET_LOCK`，测试通过注入进程内可重入锁模拟 MySQL 命名咨询锁语义；
> 生产并发保证以 MySQL `GET_LOCK('withdrawal_correction')` + 唯一约束为准。
