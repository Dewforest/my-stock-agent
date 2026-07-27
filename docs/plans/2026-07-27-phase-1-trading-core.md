# Phase 1 Trading Core Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build and verify the deterministic core shared by A/B/C/D/F: point-in-time market data, isolated accounts, A-share/US execution rules, immutable ledger, public risk controls, chronological backtesting, and a runnable fixture-based demo.

**Architecture:** Use a Python 3.11 `src/` package. Keep domain logic pure and deterministic; use DuckDB only behind repository interfaces. Strategies emit target-position intents, while risk, execution, and ledger remain the single shared path. Phase 1 deliberately excludes network data and LLM calls so the accounting and no-look-ahead guarantees can be proven first.

**Tech Stack:** Python 3.11, uv, Pydantic 2, DuckDB, Typer, pytest, pytest-cov, Ruff.

**Design source:** `docs/designs/2026-07-27-multi-strategy-paper-trading-design.md`

**Phase boundary:** This plan delivers a working deterministic vertical slice. Live data ingestion, the 200-stock universe, DeepSeek, A/B/C/D adapters, F orchestration, historical Agent replay, and forward scheduling are separate plans built on this core.

---

### Task 1: Create the Python project and quality gates

**Objective:** Create an installable package with reproducible commands for linting and tests.

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/stock_agent/__init__.py`
- Create: `tests/test_package.py`

**Step 1: Write the failing package smoke test**

```python
# tests/test_package.py
from stock_agent import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"
```

**Step 2: Add project metadata and dependencies**

`pyproject.toml` must contain:

```toml
[project]
name = "my-stock-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "duckdb>=1.3,<2",
  "pydantic>=2.11,<3",
  "typer>=0.16,<1",
]

[dependency-groups]
dev = [
  "pytest>=8.4,<9",
  "pytest-cov>=6.2,<7",
  "ruff>=0.12,<1",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/stock_agent"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --strict-markers"

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "RUF"]
```

`src/stock_agent/__init__.py`:

```python
__version__ = "0.1.0"
```

`.python-version` contains `3.11`.

`.gitignore` must exclude `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `htmlcov/`, `*.duckdb`, `.env`, `.DS_Store`.

**Step 3: Install and run the test**

Run: `uv sync --dev && uv run pytest tests/test_package.py -v`

Expected: `1 passed`.

**Step 4: Run lint**

Run: `uv run ruff check .`

Expected: `All checks passed!`.

**Step 5: Commit**

```bash
git add pyproject.toml .python-version .gitignore README.md src tests uv.lock
git commit -m "build: initialize Python trading core"
```

---

### Task 2: Define market, instrument, money, and intent models

**Objective:** Establish validated immutable contracts used by every strategy and execution component.

**Files:**
- Create: `src/stock_agent/domain/__init__.py`
- Create: `src/stock_agent/domain/models.py`
- Create: `tests/domain/test_models.py`

**Step 1: Write failing validation tests**

Cover these cases:

```python
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stock_agent.domain.models import Market, Side, StrategyIntent


def test_target_weight_must_be_between_zero_and_one() -> None:
    with pytest.raises(ValidationError):
        StrategyIntent(
            strategy_id="A",
            symbol="600519",
            market=Market.CN,
            side=Side.BUY,
            target_weight=Decimal("1.01"),
            confidence=80,
            as_of=datetime(2026, 7, 27, tzinfo=timezone.utc),
            thesis="trend",
            invalidation="trend breaks",
        )


def test_intent_requires_timezone_aware_as_of() -> None:
    with pytest.raises(ValidationError):
        StrategyIntent(
            strategy_id="A",
            symbol="600519",
            market=Market.CN,
            side=Side.BUY,
            target_weight=Decimal("0.10"),
            confidence=80,
            as_of=datetime(2026, 7, 27),
            thesis="trend",
            invalidation="trend breaks",
        )
```

Also test:

- confidence range is 0–100
- `SELL` requires target weight zero
- symbol and thesis cannot be blank
- model instances are frozen

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/domain/test_models.py -v`

Expected: FAIL because `stock_agent.domain.models` does not exist.

**Step 3: Implement minimal models**

Define:

```python
class Market(StrEnum):
    CN = "CN"
    US = "US"

class Currency(StrEnum):
    CNY = "CNY"
    USD = "USD"

class Side(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"
```

Create frozen Pydantic models for:

- `Instrument(symbol, market, currency, sector)`
- `Bar(symbol, market, session_date, open, high, low, close, volume, available_at)`
- `StrategyIntent(strategy_id, symbol, market, side, target_weight, confidence, as_of, thesis, invalidation, evidence_ids)`
- `PortfolioSnapshot(account_id, market, cash, nav, peak_nav, positions, as_of)`

Use `Decimal` for money, prices, quantities, weights, and fees. Reject naive datetimes.

**Step 4: Run tests and lint**

Run: `uv run pytest tests/domain/test_models.py -v && uv run ruff check .`

Expected: all tests pass and lint is clean.

**Step 5: Commit**

```bash
git add src/stock_agent/domain tests/domain
git commit -m "feat: define immutable trading domain models"
```

---

### Task 3: Implement point-in-time bar storage

**Objective:** Prove that a historical query cannot see bars that were not available at the simulated time.

**Files:**
- Create: `src/stock_agent/data/__init__.py`
- Create: `src/stock_agent/data/store.py`
- Create: `tests/data/test_pit_store.py`

**Step 1: Write failing PIT tests**

Use an in-memory DuckDB connection and insert two versions of a bar:

```python
def test_snapshot_excludes_future_available_record(store) -> None:
    store.append_bar(old_version_available_on_day_1)
    store.append_bar(corrected_version_available_on_day_3)

    snapshot = store.latest_bar_as_of(
        market=Market.US,
        symbol="AAPL",
        session_date=date(2026, 7, 24),
        as_of=utc("2026-07-25T00:00:00Z"),
    )

    assert snapshot.close == Decimal("333.02")
```

Add tests for:

- no record returns `None`
- latest eligible revision wins
- records from another symbol/market never leak
- querying with a naive datetime fails

**Step 2: Run tests to verify failure**

Run: `uv run pytest tests/data/test_pit_store.py -v`

Expected: FAIL because `PointInTimeStore` does not exist.

**Step 3: Implement schema and repository**

Create a `bars` table with:

- market
- symbol
- session_date
- OHLCV
- available_at
- ingested_at
- source
- source_record_id

`latest_bar_as_of()` must filter `available_at <= as_of` and select the newest eligible `available_at`, then `ingested_at`.

No strategy-facing code may access the raw DuckDB connection.

**Step 4: Run tests and lint**

Run: `uv run pytest tests/data/test_pit_store.py -v && uv run ruff check .`

Expected: all pass.

**Step 5: Commit**

```bash
git add src/stock_agent/data tests/data
git commit -m "feat: add point-in-time market data store"
```

---

### Task 4: Add market calendars and session lookup

**Objective:** Resolve the next tradable session without assuming China and US calendars are identical.

**Files:**
- Create: `src/stock_agent/market/__init__.py`
- Create: `src/stock_agent/market/calendar.py`
- Create: `tests/market/test_calendar.py`

**Step 1: Write failing tests**

Test a fixture calendar, not an external package:

```python
def test_next_session_skips_weekend_and_market_holiday() -> None:
    calendar = TradingCalendar(
        market=Market.US,
        sessions=[date(2026, 7, 24), date(2026, 7, 27)],
    )
    assert calendar.next_session(date(2026, 7, 24)) == date(2026, 7, 27)
```

Also test:

- `is_session`
- missing future session raises `NoFutureSession`
- calendars cannot mix markets

**Step 2: Run failure**

Run: `uv run pytest tests/market/test_calendar.py -v`

Expected: FAIL.

**Step 3: Implement the minimal calendar**

Store a sorted immutable tuple of session dates. Do not infer holidays from weekdays; ingestion supplies authoritative sessions later.

**Step 4: Verify**

Run: `uv run pytest tests/market/test_calendar.py -v && uv run ruff check .`

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_agent/market tests/market
git commit -m "feat: add explicit market trading calendars"
```

---

### Task 5: Implement orders and next-open execution

**Objective:** Convert an approved target-weight intent into an order and fill it only on the next tradable session.

**Files:**
- Create: `src/stock_agent/execution/__init__.py`
- Create: `src/stock_agent/execution/models.py`
- Create: `src/stock_agent/execution/simulator.py`
- Create: `tests/execution/test_next_open.py`

**Step 1: Write failing tests**

Required tests:

- a signal created after T close fills no earlier than T+1 open
- a BUY fill applies positive slippage
- a SELL fill applies negative slippage
- stale or missing next-session bar leaves the order unfilled with an explicit reason
- duplicate idempotency key cannot create two fills

Example assertion:

```python
assert fill.session_date == date(2026, 7, 27)
assert fill.price == Decimal("101.05")
assert fill.reason == "filled_next_open"
```

**Step 2: Run failure**

Run: `uv run pytest tests/execution/test_next_open.py -v`

Expected: FAIL.

**Step 3: Implement minimal execution models and simulator**

Create frozen models:

- `Order`
- `Fill`
- `ExecutionRejection`

The simulator accepts a calendar, PIT store, fee model, and slippage basis points. It must never use the signal-day close as a fill price.

**Step 4: Verify**

Run: `uv run pytest tests/execution/test_next_open.py -v && uv run ruff check .`

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_agent/execution tests/execution
git commit -m "feat: simulate next-session open execution"
```

---

### Task 6: Enforce A-share lot size, T+1, suspension, and price limits

**Objective:** Prevent fills that are illegal or impossible in the A-share market.

**Files:**
- Create: `src/stock_agent/execution/rules.py`
- Create: `tests/execution/test_cn_rules.py`
- Modify: `src/stock_agent/execution/simulator.py`

**Step 1: Write failing rule tests**

Required tests:

- a new A-share BUY quantity rounds down to a multiple of 100
- quantity below 100 is rejected
- shares bought on T cannot sell on T
- pre-existing shares may sell on T
- suspended bar rejects execution
- limit-up BUY and limit-down SELL reject execution when no executable liquidity is represented
- US quantities are not rounded to 100

**Step 2: Run failure**

Run: `uv run pytest tests/execution/test_cn_rules.py -v`

Expected: FAIL.

**Step 3: Implement rules**

Create a `MarketRuleSet` protocol and implementations:

- `ChinaAShareRules`
- `USCashEquityRules`

Track settled and unsettled quantity by acquisition session. Do not encode price-limit percentages from ticker prefixes in Phase 1; accept explicit `limit_up`, `limit_down`, and `suspended` fields in the session snapshot.

**Step 4: Verify focused and regression tests**

Run: `uv run pytest tests/execution -v && uv run ruff check .`

Expected: all execution tests pass.

**Step 5: Commit**

```bash
git add src/stock_agent/execution tests/execution
git commit -m "feat: enforce A-share execution constraints"
```

---

### Task 7: Build the immutable account ledger

**Objective:** Derive cash, positions, cost basis, and NAV entirely from append-only events.

**Files:**
- Create: `src/stock_agent/ledger/__init__.py`
- Create: `src/stock_agent/ledger/events.py`
- Create: `src/stock_agent/ledger/ledger.py`
- Create: `tests/ledger/test_ledger.py`

**Step 1: Write failing accounting tests**

Required tests:

- opening cash event creates the correct balance
- BUY fill reduces cash by notional plus fees and increases quantity
- partial SELL realizes FIFO P&L
- unrealized P&L uses PIT mark price
- reversal event cancels an erroneous event without deleting it
- account IDs and markets cannot cross-contaminate
- an event replay produces the same snapshot every time

**Step 2: Run failure**

Run: `uv run pytest tests/ledger/test_ledger.py -v`

Expected: FAIL.

**Step 3: Implement append-only events and projection**

Events:

- `CashDeposited`
- `FillBooked`
- `DividendBooked`
- `FeeBooked`
- `EventReversed`

`Ledger.snapshot(as_of, marks)` must replay eligible events and return a `PortfolioSnapshot`. Reject negative cash because leverage is disabled.

**Step 4: Verify**

Run: `uv run pytest tests/ledger/test_ledger.py -v && uv run ruff check .`

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_agent/ledger tests/ledger
git commit -m "feat: add append-only portfolio ledger"
```

---

### Task 8: Implement shared public risk controls

**Objective:** Clamp or reject strategy intents using only the approved hard limits.

**Files:**
- Create: `src/stock_agent/risk/__init__.py`
- Create: `src/stock_agent/risk/engine.py`
- Create: `tests/risk/test_engine.py`

**Step 1: Write failing tests**

Test the approved limits:

- single stock target is clamped to 15%
- sector exposure cannot exceed 30%
- at most 10 concurrent holdings per market
- one day’s new positions cannot exceed 30% of available cash
- 15% peak-to-current drawdown blocks new BUYs but permits SELLs
- 20% drawdown emits a target-risk-reduction decision
- 100% cash is valid

**Step 2: Run failure**

Run: `uv run pytest tests/risk/test_engine.py -v`

Expected: FAIL.

**Step 3: Implement the minimal risk engine**

Return a structured `RiskDecision` containing:

- original intent
- approved target weight or rejection
- rule IDs applied
- human-readable reasons

Risk must not invent a BUY, change a SELL into HOLD, or evaluate investment quality.

**Step 4: Verify**

Run: `uv run pytest tests/risk/test_engine.py -v && uv run ruff check .`

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_agent/risk tests/risk
git commit -m "feat: enforce shared portfolio risk limits"
```

---

### Task 9: Define the strategy protocol and deterministic fixture strategy

**Objective:** Prove that all future A/B/C/D/F adapters can share one strategy boundary.

**Files:**
- Create: `src/stock_agent/strategies/__init__.py`
- Create: `src/stock_agent/strategies/protocol.py`
- Create: `src/stock_agent/strategies/fixture.py`
- Create: `tests/strategies/test_protocol.py`

**Step 1: Write failing tests**

Test that a fixture strategy:

- receives only a PIT market snapshot and its own portfolio
- emits validated `StrategyIntent` objects
- cannot mutate inputs
- cannot directly book a fill or ledger event
- has a stable `strategy_id` and config version

**Step 2: Run failure**

Run: `uv run pytest tests/strategies/test_protocol.py -v`

Expected: FAIL.

**Step 3: Implement protocol and fixture strategy**

```python
from collections.abc import Sequence


class Strategy(Protocol):
    strategy_id: str

    def evaluate(self, context: StrategyContext) -> Sequence[StrategyIntent]:
        raise NotImplementedError
```

Create a deterministic moving-average fixture strategy only for integration testing. It is not A and must never be reported as an investment strategy result.

**Step 4: Verify**

Run: `uv run pytest tests/strategies/test_protocol.py -v && uv run ruff check .`

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_agent/strategies tests/strategies
git commit -m "feat: define isolated strategy adapter protocol"
```

---

### Task 10: Build the chronological backtest runner

**Objective:** Run one strategy through PIT snapshots, risk, next-open execution, and ledger without future leakage.

**Files:**
- Create: `src/stock_agent/backtest/__init__.py`
- Create: `src/stock_agent/backtest/runner.py`
- Create: `tests/backtest/test_runner.py`

**Step 1: Write failing end-to-end tests**

Create a five-session fixture where a strategy emits BUY after session 1 close and SELL after session 4 close.

Assert:

- BUY fills at session 2 open
- SELL fills at session 5 open
- a correction with `available_at` after session 3 is invisible before then
- A-share T+1 is respected
- repeated run with the same `run_id` does not duplicate fills
- final cash, quantity, realized P&L, and NAV equal hand-calculated values

**Step 2: Run failure**

Run: `uv run pytest tests/backtest/test_runner.py -v`

Expected: FAIL.

**Step 3: Implement runner**

For each session in order:

1. execute previously queued orders at the current open
2. book fills into the ledger
3. construct the PIT market and portfolio snapshots
4. call the strategy after close
5. pass intents through risk
6. queue approved orders for the next session
7. record NAV and audit events

Do not add parallelism in Phase 1.

**Step 4: Verify focused and full suite**

Run: `uv run pytest tests/backtest/test_runner.py -v && uv run pytest`

Expected: all tests pass.

**Step 5: Commit**

```bash
git add src/stock_agent/backtest tests/backtest
git commit -m "feat: add chronological no-look-ahead backtester"
```

---

### Task 11: Add performance metrics and benchmark comparison

**Objective:** Produce reproducible strategy statistics from the NAV series.

**Files:**
- Create: `src/stock_agent/evaluation/__init__.py`
- Create: `src/stock_agent/evaluation/metrics.py`
- Create: `tests/evaluation/test_metrics.py`

**Step 1: Write failing numerical tests**

Using a tiny hand-checked NAV series, test:

- total return
- annualized return
- annualized volatility
- maximum drawdown
- Sharpe with explicit risk-free rate
- Sortino
- Calmar
- benchmark excess return
- zero-volatility and insufficient-history behavior

Use `Decimal` for source values; conversion to float is allowed only inside documented statistical calculations.

**Step 2: Run failure**

Run: `uv run pytest tests/evaluation/test_metrics.py -v`

Expected: FAIL.

**Step 3: Implement metrics**

Return a typed `PerformanceReport`; never silently convert undefined metrics to zero. Use `None` for undefined values.

**Step 4: Verify**

Run: `uv run pytest tests/evaluation/test_metrics.py -v && uv run ruff check .`

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_agent/evaluation tests/evaluation
git commit -m "feat: calculate portfolio performance metrics"
```

---

### Task 12: Deliver a runnable fixture demo and CI

**Objective:** Provide a command that exercises the real vertical slice and a GitHub workflow that verifies every push.

**Files:**
- Create: `src/stock_agent/cli.py`
- Create: `tests/fixtures/market_fixture.json`
- Create: `tests/integration/test_fixture_demo.py`
- Create: `.github/workflows/ci.yml`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Step 1: Write failing CLI integration test**

Run Typer’s test runner against:

```bash
stock-agent demo --fixture tests/fixtures/market_fixture.json
```

Assert JSON output includes:

- run ID
- strategy ID marked `FIXTURE_ONLY`
- orders and fills
- ending cash and NAV
- performance metrics
- zero ledger imbalance

**Step 2: Run failure**

Run: `uv run pytest tests/integration/test_fixture_demo.py -v`

Expected: FAIL because the CLI does not exist.

**Step 3: Implement CLI and fixture**

Add:

```toml
[project.scripts]
stock-agent = "stock_agent.cli:app"
```

The demo must call the same backtest, risk, execution, and ledger components tested above. It must not print hard-coded fake results.

**Step 4: Add CI**

`.github/workflows/ci.yml` must run on push and pull request:

```yaml
- uses: astral-sh/setup-uv@v6
- run: uv python install 3.11
- run: uv sync --locked --dev
- run: uv run ruff check .
- run: uv run pytest --cov=stock_agent --cov-report=term-missing
```

**Step 5: Verify the complete artifact locally**

Run:

```bash
uv sync --dev
uv run ruff check .
uv run pytest --cov=stock_agent --cov-report=term-missing
uv run stock-agent demo --fixture tests/fixtures/market_fixture.json
```

Expected:

- lint passes
- full test suite passes
- CLI exits zero
- output is derived from fixture events
- ledger imbalance is zero

**Step 6: Commit**

```bash
git add .github README.md pyproject.toml src tests uv.lock
git commit -m "feat: deliver verified trading core demo"
```

---

## Phase 1 completion gate

Do not declare Phase 1 complete until all are true:

- `uv run ruff check .` passes
- full pytest suite passes
- fixture CLI runs successfully
- BUY-after-close fills no earlier than next open
- future revisions are invisible to earlier PIT queries
- A-share T+1 and 100-share lot rules are behaviorally verified
- ledger replay is deterministic and balanced
- five account IDs can be instantiated without state leakage
- commits are pushed to `origin/main`
- GitHub Actions reports success

## Subsequent plans

After Phase 1 passes, write and execute separate plans in this order:

1. Phase 2 — provider-backed historical ingestion, annual point-in-time universes, current A100/US100 lists
2. Phase 3 — official DeepSeek client, prompt/version audit store, A/B/C/D adapters
3. Phase 4 — F serial orchestration, representative historical Agent replay
4. Phase 5 — forward paper-trading scheduler, monitoring, reports, and operating runbook
