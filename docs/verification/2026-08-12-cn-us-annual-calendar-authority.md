# 2026 中美年度交易日历 Authority 验证

- 验证日期：2026-08-12
- 范围：SSE、SZSE、NYSE、Nasdaq 2026 年交易日、开收盘、午休、DST、半日市与跨年 next-session
- 总结论：**PARTIAL**

## Gate 结论

| Source facts | 研究结论 | 可激活 production session gate | 可用于 2026 年末跨年订单 |
|---|---|---:|---:|
| SSE 2026 官方通知与交易时段 | RESEARCH_CONFIRMED / PARTIAL | 否 | 否 |
| SZSE 2026 官方通知与交易时段 | RESEARCH_CONFIRMED / PARTIAL | 否 | 否 |
| NYSE 2026–2028 hours/calendar | RESEARCH_CONFIRMED / PARTIAL | 否 | 否 |
| Nasdaq 2026 calendar/hours | RESEARCH_CONFIRMED / PARTIAL | 否 | 否 |
| 社区 calendar 库 | INVALIDATED AS AUTHORITY | 仅作交叉检查 | 否 |

整体保持 `PARTIAL`，原因：没有完整 source-artifact manifest 和批准的 install/license profile；截至验证日，SSE/SZSE 尚无 2027 官方年度安排；四家交易所均未验证到有兼容性承诺的公共年度日历 API；官方网页/PDF 及派生 session data 的安装/再分发许可也未确认。下列内容只是官方页面事实的人工交叉核验，不是 production-authorizing verdict。

## 一手来源

### SSE

- 2026 休市通知：<https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml>
- 发布：2025-12-22，上证公告〔2025〕45号
- 官方休市公告索引：<https://www.sse.com.cn/disclosure/dealinstruc/closed/list/>
- 交易时段：<https://english.sse.com.cn/start/trading/schedule/>
- 时区：`Asia/Shanghai`
- 主板阶段：09:15–09:25 开盘竞价；09:30–11:30、13:00–14:57 连续竞价；14:57–15:00 收盘竞价。

2026 休市区间由官方通知明确给出。周末调休工作日仍不是交易日，禁止从民用工作日历推断。

### SZSE

- 2026 休市通知：<https://www.szse.cn/disclosure/notice/t20251222_618087.html>
- 发布：2025-12-22，深证会〔2025〕481号
- 官方动态日历：<https://www.szse.cn/aboutus/calendar/>
- 交易时段问答：<https://investor.szse.cn/knowledge/stock/deal/t20191204_572383.html>
- 2026 交易规则 PDF：<https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf>
- 时区与阶段同 CN 现货规则；动态站点内部接口不视为受支持公共 API。

### NYSE

- 官方 hours/calendar：<https://www.nyse.com/trade/hours-calendars>
- 2026 年度 PDF：<https://www.nyse.com/publicdocs/nyse/ICE_NYSE_2026_Yearly_Trading_Calendar.pdf>
- 本次调研下载 PDF SHA-256：`70f5577eb43e60a9dbbecaae3cec23d0f02028c05c7f175013bb3e97816d394f`
- 页面覆盖 2026、2027、2028。
- 正常核心时段：09:30–16:00 `America/New_York`。
- 2026 半日市：11-27、12-24，13:00 ET 收盘。

必须使用 IANA 时区。2026-03-06 09:30 ET 是 14:30 UTC，而 2026-03-09 09:30 ET 是 13:30 UTC；固定写死 UTC 时刻属于错误。

### Nasdaq

- 官方 holiday schedule：<https://www.nasdaq.com/market-activity/stock-market-holiday-schedule>
- 官方 trading calendar：<https://www.nasdaq.com/trading-calendar>
- 官方 holiday trading hours：<https://www.nasdaq.com/holiday-trading-hours>
- 核心时段：09:30–16:00 `America/New_York`
- 2026 半日市：11-27、12-24，13:00 ET 收盘。

延长时段不进入 v1 regular/core session。

## 第三方库结论

- `exchange-calendars 4.13.2`：Apache-2.0，2026 XSHG/XNYS/XNAS 关键日期与官方来源一致，但仅是 secondary validator。`XSES` 是新加坡交易所，不是 SZSE。
- `pandas_market_calendars 5.4.0`：MIT，2026 SSE/NYSE/NASDAQ 关键日期一致，但不是交易所 authority。

软件许可证只授权库代码，不会自动赋予交易所原始日历开放数据许可。

## Production contract

每份 source artifact 至少记录：

- source ID、交易所、文档类型、canonical URL；
- published/retrieved UTC 时间；
- HTTP status/content type/ETag/Last-Modified；
- 原始字节 SHA-256；
- parser 名称与版本；
- 许可分类和内部归档/再分发状态。

每个 session 至少记录：

- MIC、asset class、session date、IANA timezone；
- aware session segments；
- UTC 投影；
- source locator；
- generator/tzdb 版本；
- calendar digest 与 coverage through。

CN 必须表达午休；若执行层关心集合竞价，继续拆分 auction/continuous/close-auction，不能只存一个 open/close。

## Fail-closed 规则

- production `VALIDATED` 要求目标 session 与 next-session buffer 均被批准的官方 source artifacts 覆盖，并具备 parser evidence 和 install/license profile。
- `RESEARCH_CONFIRMED / PARTIAL` 只允许展示、测试模型和继续取证；不得激活 production session gate，也不得生成订单。
- 任一 market profile 的 production calendar authority 未完整闭环时，session gate 输出 `CALENDAR_AUTHORITY_BLOCKED`。相邻年度任一 represented exchange 未固化时，跨年输出 `CALENDAR_NOT_COVERED`。
- 官方来源冲突、固定 UTC、美股 DST 丢失、把调休工作日当交易日或仅凭社区库，均为 `INVALIDATED`。
- 官方原始网页/PDF默认只保存内部审计哈希和必要快照；公开再分发需另行确认条款。

## 对实现的影响

现有 `TradingCalendar(market, sessions: tuple[date, ...])` 不足。新增：

- `SourceArtifact`
- `ExchangeCalendarManifest`
- `Session`
- `SessionSegment`

旧类型仅保留为日期索引视图，不能承载 authority 声明。
