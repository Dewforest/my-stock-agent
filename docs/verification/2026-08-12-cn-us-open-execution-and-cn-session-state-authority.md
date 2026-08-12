# 中美开盘执行数据与 A 股 session-state authority 验证

- 验证日期：2026-08-12
- 范围：CN/US forward paper execution 的开盘后可见 open observation；A 股停牌、涨停、跌停状态
- 方法：只读公开网络；未读取 Keychain/浏览器 Cookie；未调用 Alpha Vantage；保存原始响应、URL、采集时间与 SHA-256
- 总结论：**PARTIAL（阻塞自动成交）**

## 1. Gate 结论

| 依赖 authority | 候选源 | 结论 | 是否可自动成交 |
|---|---|---|---|
| A 股开盘后可见 open | 东方财富 `push2delay` 分时/快照 | **PARTIAL** | 否 |
| 美股开盘后可见 open | Nasdaq.com chart JSON | **PARTIAL** | 否 |
| Nasdaq 官方开盘价 | Nasdaq Opening Cross + 官方 market-data 产品 | **PARTIAL**（产品语义成立，接入/许可未验证） | 否 |
| A 股停牌 | 上交所公开查询候选 + 交易所公告/行情产品 | **PARTIAL** | 否 |
| A 股涨停/跌停 | 东方财富快照 `f51/f52` + 当前价候选 | **INVALIDATED（作为 authority）** | 否 |
| 现有 Eastmoney/Alpha Vantage daily OHLCV | 日线 open/high/low/close/volume | **INVALIDATED** | 否 |

因此 Phase 0 gate 2 不能标记为 `VALIDATED`。订单保持 `PENDING`，并记录 retryable `EXECUTION_BLOCKED_DATA` obligation；不得生成 fill。该 obligation 只能在 authority amendment 后恢复，或通过独立的审计过期/取消状态终结。

## 2. 实测证据

### 2.1 A 股：东方财富公开行情候选

**分时 URL**

<https://push2delay.eastmoney.com/api/qt/stock/trends2/get?fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&secid=1.600519>

2026-08-12 实测 HTTP 200。响应含：

- `code=600519`、`market=1`、`name=贵州茅台`；
- `status=0`（但未找到公开字段语义/枚举合同）；
- `time=178651...`（源侧秒级时间）；
- 09:15 起逐分钟 `trends`；09:30 行存在；
- `preClose`、`trendsTotal` 等。

**快照 URL**

<https://push2delay.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f44,f45,f46,f47,f48,f51,f52,f57,f58,f60,f86,f124,f292>

实测响应含 `f46=134650`、`f51=148115`、`f52=121185`、`f86=1786515963`、`f292=2`。社区常见映射把 `f46` 解释为开盘、`f51/f52` 解释为涨跌停价，但本次未取得东方财富或交易所的公开正式 schema/枚举文档，不能把这些映射提升为 execution authority。

**判断：PARTIAL**

优点：无需登录即可读取；分时记录证明某个开盘值在开盘后可被观察，不是等到收盘才发布的 daily open。

未通过项：

1. API 是网站内部接口，未找到公开稳定 SLA、字段 schema、修订政策、频率上限或机器使用许可；
2. `push2` 主机实测出现 `RemoteDisconnected`，`push2delay` 可用，但没有错误/延迟合同；
3. 09:30 分钟值究竟是集合竞价开盘价、该分钟第一笔/最后一笔或聚合值，没有正式定义；
4. `status`/`f292` 枚举未文档化，不能据此证明停牌；
5. `f51/f52` 即使是上下限价格，也不等同“当前已封涨停/跌停”的显式状态；由当前价等于边界推导状态仍是推断，不是 authority；
6. 缺少已知停牌、涨停、跌停样本的同一时点正/负对照和后续修订观测。

东方财富隐私指引仅证明“行情资讯浏览”是不登录的基础业务，不构成 API 再利用/自动化采集/再分发许可：
<https://about.eastmoney.com/home/conceal>

### 2.2 美股：Nasdaq.com chart 候选

**URL**

<https://api.nasdaq.com/api/quote/AAPL/chart?assetclass=stocks>

2026-08-12 实测 HTTP 200，返回前一交易日 AAPL 分钟序列；其中：

- 09:29 ET：307.60
- 09:30 ET：309.3437
- 09:31 ET：308.19

这证明网页 chart API 能在事后给出 09:30 分钟观察，但未证明该值是 Nasdaq Official Opening Price，也未证明它在 09:30 后何时首次可得。

**补充 info URL**

<https://api.nasdaq.com/api/quote/AAPL/info?assetclass=stocks>

实测返回 `primaryData.isRealTime=false`。因此网页公开 JSON 不应被假定为实时、无延迟或执行级。

**判断：PARTIAL**

未通过项：分钟值语义、首发延迟、修订、稳定 schema、预算/限频、许可均无正式合同；API 也实测出现 HTTP/2 protocol error。它可用于研究/候选探针，不可直接成为 forward fill authority。

### 2.3 美股：Nasdaq Opening Cross 官方语义

官方说明：<https://www.nasdaqtrader.com/Trader.aspx?id=OpenClose>

页面明确：

- Opening Cross 在 **09:30 ET** 发生；
- MOO/LOO 明确请求在 opening price 执行；
- Opening Cross NOII 在 09:25–09:30 ET 发布。

这确立了 Nasdaq-listed 股票官方开盘价格的业务语义。NasdaqTrader 还把行情产品、技术规格、价格表、协议与 vendor 列为独立受管控产品，说明 production authority 应来自获许可 feed/vendor，而不是无合同的网页 JSON。

**判断：PARTIAL**：官方定义已确认，但本次未订阅、未连通、未验证费用、entitlement、redistribution/install policy、逐符号覆盖、可用时间和失败语义。对 NYSE-listed 标的还需要 CTA/NYSE 对应官方开盘数据；单一 Nasdaq Opening Cross 不能覆盖项目的全部 10 只美股。

### 2.4 A 股停牌：交易所候选

上交所公开查询候选：

<https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_SJ_GPSJ_SUSPENSION&pageHelp.pageSize=100&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.endPage=1>

2026-08-12 实测 HTTP 200，但响应 `result=null`、`data=null`、`total=0`，且未取得该 `sqlId` 的公开 schema/参数文档。它既不能证明“当日无停牌”，也不能验证任一已知停牌正样本。深交所旧式 ShowReport 候选实测 403/404，未形成可用合同。

**判断：PARTIAL**。交易所公告/停复牌清单是权威方向，但当前探针、全市场覆盖、发布时间、盘中临时停牌、撤销/更正和深沪统一归一化均未验证。`missing row != not suspended`。

### 2.5 A 股涨跌停状态

本次没有找到面向公开无凭证接口、同时满足以下条件的 authority：

- 显式给出 symbol/session 的 `LIMIT_UP | LIMIT_DOWN | NONE`，或给出可按正式规则无歧义计算的交易所字段；
- 覆盖主板、创业板、ST/*ST、上市初期无价格限制、除权除息及临时规则；
- 明确在开盘成交判定时已可得；
- 有修订、失败、许可和限频合同。

东方财富 `f51/f52` 加当前价只是一个技术候选。由于字段未正式文档化，而且“价格触及边界”不自动证明订单不能成交（需考虑盘口/成交与规则语义），将其直接映射为 `CnPriceLimitState` **INVALIDATED**。

## 3. 时间与修订合同（production 必须采用）

每次 observation/state 至少保存：

- `session_date`, `market`, `symbol`；
- `event_at`：交易所事件/成交时间（例如 09:30 Opening Cross）；
- `source_published_at`：仅当来源明确提供且定义清楚；
- `observed_at`：本机收到完整 HTTP/feed 响应的 UTC 时刻；
- `available_at`：策略允许使用的最早时刻。没有可证明的源发布时间时，保守取 `observed_at`，不得回填为 09:30；
- `ingested_at`：原始响应原子持久化完成的 UTC 时刻，必须 `>= observed_at >= available_at`（当 `available_at=observed_at` 时相等可接受）；
- source URL/product/version、请求参数、HTTP 状态、response hash、source record id；
- `revision_no`/supersedes hash、首次值与最新值，禁止覆盖原记录。

开盘值在开盘后才能观察，因此 forward 模型应允许 `available_at > intended_open_at`，并以 `intended_open_at` 为 execution `effective_at`、实际落账时刻为 `recorded_at`。当前 `BacktestSession` 对 open bar 施加 `bar.available_at <= open_at`，不适合作为 forward observation 合同；forward runtime 应使用独立 `ExecutionOpenObservation`，不要复用/放宽历史 backtest 模型来伪造“开盘时已知”。

若来源随后改值：保留 revision，但已经 FINALIZED 的 paper fill 不静默重写；进入 reconciliation，按版本化政策决定保留、冲正或标记 disputed。

## 4. 建议预算、限频与失败语义

在缺少供应商正式配额前，以下只能作为保守客户端预算，**不是来源授权**：

- 东方财富：每个 CN open batch 最多 10 symbols；串行或并发 2；每请求最多 1 次延迟重试；相同 symbol/session 成功后不重复取；全日预留 10 次恢复请求。
- Nasdaq 网页 API：仅 proof/diagnostic，不进入 production 自动成交预算。
- 正式交易所/vendor feed：严格按 entitlement、连接数、消息率和 display/non-display 条款实现，预算写入 provider profile。

规范化失败：

- `THROTTLED`（429 或明确限频响应）：当日/规定窗口 circuit open；
- `AUTH/ENTITLEMENT`：不可重试，阻塞 provider；
- `DNS/TIMEOUT/RESET/HTTP_5XX`：最多一次持久化延迟重试；
- `HTTP_403/404`：不可把 HTML/空响应当数据；配置/schema 错误；
- `SCHEMA/IDENTITY/NUMERIC`：不可重试；隔离整份响应；
- `MISSING_SYMBOL`、`NULL_DATA`、`UNKNOWN_STATUS_ENUM`：authority missing，不得解释为正常交易；
- CN 任一 open/suspension/limit authority 缺失：订单保持 `PENDING` 并记录 `EXECUTION_BLOCKED_DATA`；不得 fill；
- 已知 suspended：`FINALIZED_REJECTED/SUSPENDED`；
- 显式 limit-up buy / limit-down sell：`FINALIZED_REJECTED/PRICE_LIMIT`（只有被选 authority 的正式语义支持时）。

## 5. 推荐落地路径

1. **US**：向 Nasdaq/NYSE/CTA 授权 vendor 取得覆盖全部 universe 的 official open/first eligible trade feed；记录产品名、协议、non-display 使用权、价格、配额、timestamp 与 correction 消息。完成开盘后正样本、halt/无成交和 correction 测试后才可 `VALIDATED`。
2. **CN**：优先上证/深证信息公司授权 Level-1/逐笔或获许可 vendor；要求明确证券状态、停牌、当日价格上/下限及开盘成交字段。分别验证正常、全天停牌、盘中临停、涨停、跌停、ST、创业板、新股/无涨跌幅限制样本。
3. 每个市场保存 whole-response fixtures 与哈希；做 T0、T+1min、午间/收盘、T+1 日重复抓取，以验证首次可用时间和 revisions。
4. 在上述两项完成前，保持设计 §8 的 fail-closed 规则，不用 daily OHLCV、零成交、缺行或价格相等来猜状态。

## 6. 本次证据文件

目录：`docs/verification/execution-authority-2026-08-12/`

- `eastmoney-cn-600519-trends.json`
- `eastmoney-cn-600519-quote.json`
- `nasdaq-us-aapl-chart.json`
- `nasdaq-us-aapl-info.json`
- `sse-suspension-query.json`
- `manifest.json`（URL、HTTP status、observed/ingested time、bytes、SHA-256）

这些 fixtures 只证明本次公开端点响应及候选字段，不提升其许可或权威等级。