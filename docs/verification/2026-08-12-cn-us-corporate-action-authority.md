# 中美公司行动 Authority 验证

- 验证日期：2026-08-12
- 范围：现金分红、送股/转增、拆并股、symbol/name change、合并、分拆、退市与上市转移
- 总结论：**PARTIAL（阻塞 position-bearing 自动激活）**

## 核心判断

公司行动存在两类 authority，不能混为一个数据字段：

1. 法律事实 authority：发行人法定公告、SEC EDGAR、SSE/SZSE/巨潮披露；
2. 市场处理 authority：挂牌交易所或 FINRA 对 effective date、symbol、交易/除权处理的正式记录。

免费聚合商可做发现和历史交叉核验，但不能取代市场处理 authority。二者冲突时保留两份 observation 并进入 reconciliation，不静默覆盖。

## 来源矩阵

| 来源 | 能力 | 验证结论 |
|---|---|---|
| SEC EDGAR APIs | 合并、私有化、退市、改名、重大分红/拆股法律披露；`acceptanceDateTime` 可作为首次公开可得时间 | VALIDATED：法律事实；PARTIAL：标准化事件 |
| Nasdaq Trader / Daily List Data Feed | Nasdaq 分红、拆并股、代码/名称变化、上市/退市的市场生效处理 | Authority 类别 VALIDATED；旧免费 DailyList 页面 INVALIDATED；生产 feed 接入/许可待确认 |
| NYSE/ICE reference-data product | NYSE 公司行动、代码变化、退市 | Authority 类别 VALIDATED；旧公开 URL 404；产品接入/许可待确认 |
| FINRA Daily List | OTC 分红、拆并股、名称/代码变化、删除 | OTC authority 类别 VALIDATED；旧公开端点失效，现行接入 PARTIAL |
| SSE 上市公司公告 | A 股权益分派、送转、改名、合并、终止上市等法定公告 | VALIDATED：公告 authority；机器接口与再分发 PARTIAL |
| SZSE 上市公司公告 | 同上 | VALIDATED：公告 authority；机器接口与再分发 PARTIAL |
| 巨潮资讯 | 上市公司法定公告及历史公告 | VALIDATED：公告 authority；未承诺稳定开放 API |
| 中国结算 | 登记、派息到账、证券登记等结算事实 | Authority 类别 VALIDATED；公开统一接入 PARTIAL |
| Eastmoney | A 股分红送转历史及日期候选 | PARTIAL，仅 fallback/交叉核验 |
| Alpha Vantage | 美股 dividends、splits、listing/delisting status | PARTIAL，不覆盖完整 merger/symbol chain，也非交易所 authority |

## 一手来源

- SEC API：<https://www.sec.gov/edgar/sec-api-documentation>
- Nasdaq Trader：<https://www.nasdaqtrader.com/>
- NYSE market data：<https://www.nyse.com/market-data>
- FINRA transparency/reporting：<https://www.finra.org/filing-reporting/market-transparency-reporting>
- SSE 公告：<https://www.sse.com.cn/disclosure/listedinfo/announcement/>
- SZSE 公告：<https://www.szse.cn/disclosure/listed/notice/index.html>
- 巨潮资讯：<http://www.cninfo.com.cn/new/index>
- Alpha Vantage docs：<https://www.alphavantage.co/documentation/>

SEC 无需 API key，但必须使用可识别 User-Agent 并遵守 fair-access。交易所/FINRA 结构化 feed 的价格、entitlement、non-display 使用和再分发权需向供应方确认。

## Canonical event contract

每个事件至少包含：

- 永久 issuer/security identity；symbol 只是版本化属性；
- `symbol_before` / `symbol_after`；
- event type 与 status；
- 精确现金、币种和有理数股份比例；
- announcement/declaration/ex/record/payable/effective/last-trade 日期分别保存；
- source-published、first-observed、ingested aware UTC 时间；
- authority class、publisher、source event ID、URL、原始文档哈希、许可 profile；
- append-only revision、supersedes、correction reason。

事件类型至少覆盖：

- `CASH_DIVIDEND`
- `STOCK_DIVIDEND`
- `BONUS_ISSUE`
- `CAPITALIZATION_ISSUE`
- `FORWARD_SPLIT`
- `REVERSE_SPLIT`
- `SYMBOL_CHANGE`
- `NAME_CHANGE`
- `MERGER_CASH`
- `MERGER_STOCK`
- `SPINOFF`
- `DELISTING`
- `LISTING_TRANSFER`

状态至少覆盖 `ANNOUNCED / CONFIRMED / EFFECTIVE / CANCELLED / SUPERSEDED`。退市必须区分 voluntary、regulatory、merger、bankruptcy 和 listing transfer。

## Fail-closed 规则

- `announcement_date`、source-published 和 first-observed 不能互相替代。
- ex/record/payable/effective 日期不能压成一个通用 `date`。
- 拆股比例不得使用浮点数。
- 更正只追加 revision，不覆盖旧版本。
- 原始公告/PDF/JSON、响应头、抓取时间和哈希必须保留。
- 未取得完整市场处理 authority 时，不允许 position-bearing 自动激活。
- 不得用价格跳变、raw/adjusted 差异或聚合商单条记录自动修复数量、成本、现金、NAV 或 P&L。

## 推荐路径

### 无商业数据合同的最低能力

- CN：SSE/SZSE/巨潮公告作为法律事实；Eastmoney 仅发现/历史交叉核验。
- US：SEC submissions/filings + issuer IR 作为法律事实。

此路径不能保证标准化 ex-date、symbol chain、全部退市和市场实际处理，因此只能发现、告警和人工 reconciliation。

### 完整自动持仓能力

- US：挂牌交易所 corporate-action/reference feed；OTC 另接 FINRA Daily List。
- CN：交易所或获授权数据商的结构化公司行动与证券状态数据；公告原文继续作为审计依据。

在供应商产品、许可、预算和 whole-response fixture 均验证前，结论保持 `PARTIAL`。
