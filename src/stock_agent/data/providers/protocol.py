from typing import Protocol

from stock_agent.data.providers.models import DailyBarRequest, FetchedDailyBar


class HistoricalDailyBarProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def fetch_daily_bars(
        self, request: DailyBarRequest
    ) -> tuple[FetchedDailyBar, ...]: ...
