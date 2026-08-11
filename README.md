# my-stock-agent

面向 A/B/C/D/F 策略的中美股模拟交易系统。项目用于在隔离账户中验证多策略的交易、风控与记账行为；**不连接真实券商，也不会提交真实订单**。

## Phase 1

Phase 1 聚焦确定性交易内核：以可复现、无网络依赖的方式建立后续策略共享的基础能力。本阶段不实现任何真实交易接入。

## 开发

要求 Python 3.11 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --dev
uv run pytest
uv run ruff check .
```
