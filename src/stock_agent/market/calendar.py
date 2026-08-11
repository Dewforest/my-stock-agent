from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from stock_agent.domain import Market


class NoFutureSession(LookupError):
    """Raised when a calendar has no listed session after a date."""


@dataclass(frozen=True)
class TradingCalendar:
    market: Market
    sessions: tuple[date, ...]

    def __init__(self, market: Market, sessions: Iterable[date]) -> None:
        if not isinstance(market, Market):
            raise TypeError("market must be a Market")

        materialized = tuple(sessions)
        if any(type(session) is not date for session in materialized):
            raise TypeError("sessions must contain only plain date values")

        ordered = tuple(sorted(materialized))
        if any(left == right for left, right in pairwise(ordered)):
            raise ValueError("sessions must not contain duplicate dates")

        object.__setattr__(self, "market", market)
        object.__setattr__(self, "sessions", ordered)

    def is_session(self, value: date) -> bool:
        if type(value) is not date:
            raise TypeError("value must be a plain date")
        index = bisect_left(self.sessions, value)
        return index < len(self.sessions) and self.sessions[index] == value

    def next_session(self, after: date) -> date:
        if type(after) is not date:
            raise TypeError("after must be a plain date")
        index = bisect_right(self.sessions, after)
        if index == len(self.sessions):
            raise NoFutureSession(f"no future session for market {self.market} after {after}")
        return self.sessions[index]
