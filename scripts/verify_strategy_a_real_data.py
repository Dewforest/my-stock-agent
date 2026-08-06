from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Never

if __package__:
    from .strategy_a_live_schedule import STRATEGY_A_SCHEDULE, STRATEGY_A_SCHEDULE_ID
    from .verify_live_market_data import atomic_write_evidence
else:
    from strategy_a_live_schedule import STRATEGY_A_SCHEDULE, STRATEGY_A_SCHEDULE_ID
    from verify_live_market_data import atomic_write_evidence

from stock_agent.audit import tagged_sha256
from stock_agent.backtest import ChronologicalBacktestRunner, build_real_data_backtest_spec
from stock_agent.data import PointInTimeStore
from stock_agent.domain import Bar, Instrument, Side
from stock_agent.execution import FillStatus
from stock_agent.market import TradingCalendar
from stock_agent.risk import RiskEngine
from stock_agent.strategies.llm_contract import (
    LLMDecisionRequest,
    LLMInvocationMode,
    LLMRunAttestation,
    StrategyAConfig,
)
from stock_agent.strategies.llm_journal import LLMDecisionJournal
from stock_agent.strategies.llm_provider import (
    ExactModelIdentityPolicy,
    InvocationStart,
    RawLLMResponse,
    RecordedLLMDecisionProvider,
    ReplayLLMDecisionProvider,
)
from stock_agent.strategies.strategy_a import BoundedLLMStrategyA

_SCHEMA_VERSION = "strategy-a-real-data-verification/v1"
_FIXTURE_SCHEMA_VERSION = "strategy-a-provider-fixture/v1"
_PIT_POLICY = "current-view-baseline/v1"
_PRICE_POLICY = "raw-unadjusted/no-corporate-actions/v1"
_PROMPT = "Select exactly one legal action per sorted Strategy A candidate envelope."
_PROMPT_DIGEST = "prompt-sha256:" + hashlib.sha256(_PROMPT.encode()).hexdigest()
_MODEL_IDENTITY = "recorded-fixture/strategy-a-request-aware"
_MODEL_REVISION = "v1"
_MODEL_POLICY_ID = "recorded-fixture-exact-identity/v1"
_PROMPT_TEMPLATE_ID = "strategy-a-bounded-selection/v1"
_INITIAL_CASH = Decimal("100000")
_RUN_ID = "strategy-a-real-data-proof-v1"
_ACCOUNT_ID = "strategy-a-real-data-proof"
_DECISION_DATE = "2026-07-28"
_EXECUTION_DATE = "2026-07-29"
_NORMALIZED_RESPONSE_DIGEST = (
    "nasdaq-normalized-response-sha256:"
    "ec762718b31542917a01572c99f1994df0371f7c6b10fcae131f6481371ff0eb"
)

_FIXTURE_KEYS = {
    "schema_version",
    "provider_id",
    "provider_schema",
    "source_host",
    "source_path",
    "request_parameters",
    "observed_at_utc",
    "response_total_records",
    "normalized_response_digest",
    "selection_policy",
    "selection_start",
    "selection_end",
    "market",
    "symbol",
    "currency",
    "sector",
    "timezone",
    "open_time",
    "close_time",
    "pit_knowledge_policy",
    "market_data_price_policy",
    "dataset_digest",
    "response_rows",
    "rows",
}
_REQUEST_KEYS = {"assetclass", "fromdate", "todate", "limit"}
_ROW_KEYS = {"date", "open", "high", "low", "close", "volume", "source_record_id"}
_RESPONSE_ROW_KEYS = {"date", "open", "high", "low", "close", "volume"}
_RELEVANT_EXACT_FILES = {
    "scripts/strategy_a_live_schedule.py",
    "scripts/verify_strategy_a_real_data.py",
    "src/stock_agent/data/policies.py",
    "src/stock_agent/data/store.py",
    "tests/fixtures/strategy_a/nasdaq-ibm-2026-07.json",
}
_RELEVANT_DIRECTORIES = (
    "src/stock_agent/account",
    "src/stock_agent/backtest",
    "src/stock_agent/execution",
    "src/stock_agent/risk",
    "src/stock_agent/strategies",
)
_SECRET_PATTERNS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN RSA " + b"PRIVATE KEY-----",
    b"sk-" + b"proj-",
    b"AK" + b"IA",
)


class StrategyARealDataVerificationError(RuntimeError):
    """Fail-closed error for malformed fixture, lifecycle, replay, or evidence."""


def _fail(message: str) -> Never:
    raise StrategyARealDataVerificationError(message)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256(tag: str, value: object) -> str:
    return f"{tag}-sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _decimal_text(value: Decimal) -> str:
    return str(value)


def _strict_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(f"{label} must have exact keys")
    return value


def _parse_utc(value: object, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        _fail(f"{label} must be a UTC Z datetime")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(f"{label} is invalid")
    if parsed.utcoffset() != timedelta(0):
        _fail(f"{label} must be UTC")
    return parsed


def _strict_decimal(value: object, label: str, *, positive: bool = False) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        _fail(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _fail(f"{label} is invalid")
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        _fail(f"{label} is out of range")
    return parsed


def _load_fixture(path: Path) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("fixture cannot be read as JSON")
    fixture = _strict_object(raw, _FIXTURE_KEYS, "fixture")
    expected_scalars = {
        "schema_version": _FIXTURE_SCHEMA_VERSION,
        "provider_id": STRATEGY_A_SCHEDULE.provider_id,
        "provider_schema": "nasdaq-historical/v1",
        "source_host": "api.nasdaq.com",
        "source_path": "/api/quote/IBM/historical",
        "observed_at_utc": "2026-08-06T10:47:22Z",
        "market": STRATEGY_A_SCHEDULE.market.value,
        "symbol": STRATEGY_A_SCHEDULE.symbol,
        "currency": STRATEGY_A_SCHEDULE.currency.value,
        "sector": STRATEGY_A_SCHEDULE.sector,
        "timezone": "America/New_York",
        "open_time": "09:30:00",
        "close_time": "16:00:00",
        "pit_knowledge_policy": _PIT_POLICY,
        "market_data_price_policy": _PRICE_POLICY,
        "dataset_digest": STRATEGY_A_SCHEDULE.fixture_dataset_digest,
    }
    if any(
        type(fixture.get(key)) is not str or fixture.get(key) != expected
        for key, expected in expected_scalars.items()
    ):
        _fail("fixture metadata does not match the frozen provider schedule")
    request = _strict_object(fixture["request_parameters"], _REQUEST_KEYS, "request parameters")
    if request != {
        "assetclass": "stocks",
        "fromdate": "2026-07-20",
        "todate": "2026-08-05",
        "limit": "30",
    } or any(type(value) is not str for value in request.values()):
        _fail("fixture request metadata is invalid")
    if (
        type(fixture["response_total_records"]) is not int
        or fixture["response_total_records"] != 13
        or fixture["normalized_response_digest"] != _NORMALIZED_RESPONSE_DIGEST
        or fixture["selection_policy"]
        != "minimal-contiguous-window-with-natural-offensive-signal/v1"
        or fixture["selection_start"] != "2026-07-24"
        or fixture["selection_end"] != "2026-07-30"
    ):
        _fail("fixture response selection metadata is invalid")
    _parse_utc(fixture["observed_at_utc"], "observed_at_utc")

    raw_response_rows = fixture["response_rows"]
    if type(raw_response_rows) is not list or len(raw_response_rows) != 13:
        _fail("fixture must retain all thirteen normalized provider response rows")
    response_rows: list[object] = raw_response_rows
    derived_rows: list[dict[str, object]] = []
    for index, value in enumerate(response_rows):
        response_row = _strict_object(value, _RESPONSE_ROW_KEYS, f"response row {index}")
        if any(type(response_row[key]) is not str for key in _RESPONSE_ROW_KEYS):
            _fail("provider response row values must all be strings")
        try:
            session_date = datetime.strptime(str(response_row["date"]), "%m/%d/%Y").date()
        except ValueError:
            _fail("provider response row date is invalid")
        if not (datetime(2026, 7, 24).date() <= session_date <= datetime(2026, 7, 30).date()):
            continue
        normalized = {
            key: str(response_row[key]).replace("$", "").replace(",", "")
            for key in ("open", "high", "low", "close", "volume")
        }
        source_id = tagged_sha256(
            "provider-payload",
            (
                "nasdaq-historical/v1",
                STRATEGY_A_SCHEDULE.symbol,
                session_date.isoformat(),
                normalized["open"],
                normalized["high"],
                normalized["low"],
                normalized["close"],
                normalized["volume"],
            ),
        )
        derived_rows.append(
            {
                "date": session_date.isoformat(),
                **normalized,
                "source_record_id": source_id,
            }
        )
    if _sha256("nasdaq-normalized-response", response_rows) != _NORMALIZED_RESPONSE_DIGEST:
        _fail("normalized provider response digest is stale")
    derived_rows.sort(key=lambda row: str(row["date"]))

    raw_rows = fixture["rows"]
    if type(raw_rows) is not list or len(raw_rows) != 5:
        _fail("fixture must contain exactly five rows")
    rows: list[dict[str, object]] = []
    for index, value in enumerate(raw_rows):
        row = _strict_object(value, _ROW_KEYS, f"row {index}")
        if any(type(row[key]) is not str for key in _ROW_KEYS):
            _fail("fixture row values must all be strings")
        values = {
            key: _strict_decimal(row[key], f"row {index} {key}", positive=key != "volume")
            for key in ("open", "high", "low", "close", "volume")
        }
        if (
            values["low"] > min(values["open"], values["close"])
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > values["high"]
        ):
            _fail("fixture OHLC is inconsistent")
        expected_source_id = tagged_sha256(
            "provider-payload",
            (
                "nasdaq-historical/v1",
                STRATEGY_A_SCHEDULE.symbol,
                row["date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            ),
        )
        if not hmac.compare_digest(str(row["source_record_id"]), expected_source_id):
            _fail("fixture source record ID does not match its provider row")
        rows.append(row)
    dates = tuple(row["date"] for row in rows)
    expected_dates = tuple(
        row.session_date.isoformat() for row in STRATEGY_A_SCHEDULE.schedule.sessions
    )
    if dates != expected_dates:
        _fail("fixture sessions are not the exact sorted schedule")
    if rows != derived_rows:
        _fail("selected fixture rows do not derive from the retained provider response")
    source_ids = tuple(row["source_record_id"] for row in rows)
    if source_ids != STRATEGY_A_SCHEDULE.expected_source_record_ids:
        _fail("fixture source record IDs are stale or invalid")
    digest = _sha256("dataset", rows)
    if not hmac.compare_digest(str(fixture["dataset_digest"]), digest):
        _fail("fixture dataset digest is stale")
    return fixture, tuple(rows)


def _relevant_source_files(repository_root: Path) -> tuple[str, ...]:
    files = set(_RELEVANT_EXACT_FILES)
    for relative_directory in _RELEVANT_DIRECTORIES:
        directory = repository_root / relative_directory
        files.update(
            path.relative_to(repository_root).as_posix()
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return tuple(sorted(files))


def compute_relevant_source_digest(repository_root: Path) -> str:
    digest = hashlib.sha256(b"strategy-a-real-data-relevant-source/v1\x00")
    try:
        for relative in _relevant_source_files(repository_root):
            encoded = relative.encode()
            content = (repository_root / relative).read_bytes()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except OSError:
        _fail("relevant source digest cannot be computed")
    return f"relevant-source-sha256:{digest.hexdigest()}"


def _tracked_secret_scan(repository_root: Path) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
        tracked = tuple(item for item in completed.stdout.split(b"\x00") if item)
        findings: list[str] = []
        scanned = 0
        for raw_path in tracked:
            path = repository_root / raw_path.decode("utf-8")
            try:
                content = path.read_bytes()
            except OSError:
                _fail("tracked-secret scan could not read a tracked file")
            scanned += 1
            if any(pattern in content for pattern in _SECRET_PATTERNS):
                findings.append(raw_path.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, UnicodeError):
        _fail("tracked-secret scan failed")
    if findings:
        _fail("tracked-secret scan found credential-shaped content")
    return {
        "passed": True,
        "scope": "git-tracked-and-untracked-nonignored",
        "scanned_file_count": scanned,
        "finding_count": 0,
    }


def _config() -> StrategyAConfig:
    return StrategyAConfig(
        config_version="strategy-a-real-data/v1",
        short_window=2,
        long_window=3,
        volume_window=2,
        volume_confirmation_threshold=Decimal("1"),
        offensive_target_weight=Decimal("0.10"),
        neutral_target_weight=Decimal("0.05"),
        model_identity_policy_id=_MODEL_POLICY_ID,
        prompt_template_id=_PROMPT_TEMPLATE_ID,
        prompt_template_digest=_PROMPT_DIGEST,
    )


def _policy() -> ExactModelIdentityPolicy:
    return ExactModelIdentityPolicy(
        policy_id=_MODEL_POLICY_ID,
        model_identity=_MODEL_IDENTITY,
        model_revision=_MODEL_REVISION,
    )


class _DeterministicInvocationBoundary:
    def __init__(self, run_at: datetime) -> None:
        self._count = 0
        self._base = run_at.astimezone(UTC) - timedelta(minutes=10)

    def begin(self) -> InvocationStart:
        self._count += 1
        return InvocationStart(
            attempt_id=f"strategy-a-real-data-attempt-{self._count}",
            started_at=self._base + timedelta(seconds=self._count * 2),
        )

    def end_at(self, invocation: InvocationStart) -> datetime:
        return invocation.started_at + timedelta(seconds=1)


class _RequestAwareRecordedFixtureTransport:
    def __init__(self) -> None:
        self.requests: list[LLMDecisionRequest] = []

    def invoke(self, request: LLMDecisionRequest) -> RawLLMResponse:
        if not request.candidates:
            _fail("recorded fixture transport received no candidates")
        selections: list[dict[str, object]] = []
        for candidate in request.candidates:
            legal = tuple(target.action for target in candidate.action_targets)
            if (
                candidate.as_of.date().isoformat() == _DECISION_DATE
                and candidate.regime.value == "OFFENSIVE"
                and Side.BUY in legal
            ):
                action = Side.BUY
            elif Side.HOLD in legal:
                action = Side.HOLD
            else:
                action = legal[0]
            if action not in legal:
                _fail("recorded fixture selected an illegal action")
            selections.append(
                {
                    "symbol": candidate.symbol,
                    "action": action.value,
                    "confidence": 91,
                    "thesis": f"recorded bounded {action.value.lower()} selection",
                    "invalidation": "candidate envelope or frozen evidence changes",
                }
            )
        self.requests.append(request)
        return RawLLMResponse(
            payload={
                "schema_version": "llm-decision-response/v1",
                "request_fingerprint": request.request_fingerprint,
                "selections": selections,
                "provider_response_id": f"recorded-fixture-response-{len(self.requests)}",
            },
            model_identity=_MODEL_IDENTITY,
            model_revision=_MODEL_REVISION,
        )


def _strategy(config: StrategyAConfig, provider: object) -> BoundedLLMStrategyA:
    return BoundedLLMStrategyA(
        config=config,
        provider=provider,  # type: ignore[arg-type]
        model_identity_policy_id=config.model_identity_policy_id,
        prompt_template_id=config.prompt_template_id,
        prompt_template_digest=config.prompt_template_digest,
    )


def _store_and_spec(rows: tuple[dict[str, object], ...]) -> tuple[PointInTimeStore, object]:
    store = PointInTimeStore()
    observed_at = datetime(2026, 8, 6, 10, 47, 22, tzinfo=UTC)
    schedule_rows = STRATEGY_A_SCHEDULE.schedule.sessions
    for fixture_row, schedule_row in zip(rows, schedule_rows, strict=True):
        store.append_bar(
            Bar(
                symbol=STRATEGY_A_SCHEDULE.symbol,
                market=STRATEGY_A_SCHEDULE.market,
                session_date=schedule_row.session_date,
                open=Decimal(str(fixture_row["open"])),
                high=Decimal(str(fixture_row["high"])),
                low=Decimal(str(fixture_row["low"])),
                close=Decimal(str(fixture_row["close"])),
                volume=Decimal(str(fixture_row["volume"])),
                available_at=schedule_row.close_at,
            ),
            ingested_at=observed_at,
            source=STRATEGY_A_SCHEDULE.provider_id,
            source_record_id=str(fixture_row["source_record_id"]),
        )
    config = _config()
    spec = build_real_data_backtest_spec(
        store=store,
        schedule=STRATEGY_A_SCHEDULE.schedule,
        run_id=_RUN_ID,
        account_id=_ACCOUNT_ID,
        instruments=(
            Instrument(
                symbol=STRATEGY_A_SCHEDULE.symbol,
                market=STRATEGY_A_SCHEDULE.market,
                currency=STRATEGY_A_SCHEDULE.currency,
                sector=STRATEGY_A_SCHEDULE.sector,
            ),
        ),
        initial_cash=_INITIAL_CASH,
        strategy_config_version=config.config_version,
    )
    if spec.pit_knowledge_policy != _PIT_POLICY or spec.market_data_price_policy != _PRICE_POLICY:
        store.close()
        _fail("real-data policy pair changed")
    return store, spec


def _runner(store: PointInTimeStore, strategy: object) -> ChronologicalBacktestRunner:
    return ChronologicalBacktestRunner(
        store=store,
        calendar=TradingCalendar(
            STRATEGY_A_SCHEDULE.market,
            tuple(row.session_date for row in STRATEGY_A_SCHEDULE.schedule.sessions),
        ),
        strategy=strategy,  # type: ignore[arg-type]
        risk_engine=RiskEngine(),
    )


def _model_json(value: Any) -> dict[str, object]:
    dumped = value.model_dump(mode="json")
    if type(dumped) is not dict:
        _fail("model summary was not an object")
    return dumped


def _source_record_digest(ids: tuple[str, ...]) -> str:
    return (
        "source-record-ids-sha256:"
        + hashlib.sha256(
            json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _summarize_attestation(value: LLMRunAttestation) -> dict[str, object]:
    return {
        "attestation_id": value.attestation_id,
        "execution_label": value.execution_label,
        "mode": value.invocation_mode.value,
        "decision_ids": list(value.decision_ids),
        "occurred_at": value.occurred_at.isoformat().replace("+00:00", "Z"),
        "outside_backtest_result": True,
    }


def _assemble_evidence(
    *,
    fixture: dict[str, object],
    record_result: Any,
    replay_result: Any,
    requests: tuple[LLMDecisionRequest, ...],
    decisions: tuple[Any, ...],
    attempts: tuple[Any, ...],
    attestations: tuple[LLMRunAttestation, ...],
    repository_root: Path,
    run_at: datetime,
) -> dict[str, object]:
    if not requests or not decisions or not attempts:
        _fail("candidate requests and recorded decisions must be nonempty")
    decision_request = next(
        (item for item in requests if item.as_of.date().isoformat() == _DECISION_DATE), None
    )
    if decision_request is None or not decision_request.candidates:
        _fail("offensive decision request was not resolved")
    candidate = decision_request.candidates[0]
    record = next(
        (
            item
            for item in decisions
            if item.request_fingerprint == decision_request.request_fingerprint
        ),
        None,
    )
    if record is None or not record.selections:
        _fail("decision record resolution is missing")
    selection = record.selections[0]
    decision_session = next(
        (
            item
            for item in record_result.sessions
            if item.session_date.isoformat() == _DECISION_DATE
        ),
        None,
    )
    execution_session = next(
        (
            item
            for item in record_result.sessions
            if item.session_date.isoformat() == _EXECUTION_DATE
        ),
        None,
    )
    if decision_session is None or execution_session is None:
        _fail("trade lifecycle sessions are missing")
    if not decision_session.intents or not decision_session.risk_decisions:
        _fail("BUY intent or risk decision is missing")
    buy_intent = next((item for item in decision_session.intents if item.side is Side.BUY), None)
    pending = next(
        (item for item in decision_session.submission_results if item.status is FillStatus.PENDING),
        None,
    )
    fill = next(
        (
            item
            for item in execution_session.execution_results
            if item.status is FillStatus.FILLED and item.side is Side.BUY
        ),
        None,
    )
    if buy_intent is None or pending is None or fill is None:
        _fail("genuine BUY, pending order, and next-open fill are required")
    if fill.session_date.isoformat() != _EXECUTION_DATE:
        _fail("BUY did not fill at the following session open")
    record_bytes = record_result.model_dump_json().encode()
    replay_bytes = replay_result.model_dump_json().encode()
    record_intents = _canonical_json(
        [[_model_json(intent) for intent in session.intents] for session in record_result.sessions]
    )
    replay_intents = _canonical_json(
        [[_model_json(intent) for intent in session.intents] for session in replay_result.sessions]
    )
    if (
        record_bytes != replay_bytes
        or record_intents != replay_intents
        or record_result != replay_result
    ):
        _fail("fresh replay does not byte-match the recorded result")
    if not record_result.final_lots or not record_result.final_snapshot.positions:
        _fail("filled BUY did not produce a final lot and position")
    if record_result.final_snapshot.nav == _INITIAL_CASH:
        _fail("final NAV is unchanged")
    source_ids = tuple(
        sorted(
            {
                revision.source_record_id
                for session in record_result.sessions
                for revision in session.selected_revisions
            }
        )
    )
    if source_ids != tuple(sorted(STRATEGY_A_SCHEDULE.expected_source_record_ids)):
        _fail("result source traceability is incomplete")
    config = _config()
    config_json = config.model_dump(mode="json")
    action_targets = [
        {"action": target.action.value, "target_weight": _decimal_text(target.target_weight)}
        for target in candidate.action_targets
    ]
    return {
        "schema_version": _SCHEMA_VERSION,
        "status": "success",
        "run_at_utc": run_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "relevant_source_digest": compute_relevant_source_digest(repository_root),
        "fixture": {
            "path": "tests/fixtures/strategy_a/nasdaq-ibm-2026-07.json",
            "schema_version": fixture["schema_version"],
            "dataset_digest": fixture["dataset_digest"],
            "provider_id": fixture["provider_id"],
            "provider_schema": fixture["provider_schema"],
            "source_host": fixture["source_host"],
            "source_path": fixture["source_path"],
            "request_parameters": fixture["request_parameters"],
            "observed_at_utc": fixture["observed_at_utc"],
            "response_total_records": fixture["response_total_records"],
            "normalized_response_digest": fixture["normalized_response_digest"],
            "selection_policy": fixture["selection_policy"],
            "selection_start": fixture["selection_start"],
            "selection_end": fixture["selection_end"],
            "row_count": 5,
        },
        "schedule": {
            "id": STRATEGY_A_SCHEDULE_ID,
            "provenance_id": STRATEGY_A_SCHEDULE.provenance_id,
            "timezone": "America/New_York",
            "open_time": "09:30:00",
            "close_time": "16:00:00",
            "sessions": [
                {
                    "date": row.session_date.isoformat(),
                    "open_at": row.open_at.isoformat(),
                    "close_at": row.close_at.isoformat(),
                }
                for row in STRATEGY_A_SCHEDULE.schedule.sessions
            ],
        },
        "source_records": {
            "ids": list(source_ids),
            "digest": _source_record_digest(source_ids),
            "selected_revision_count": sum(
                len(session.selected_revisions) for session in record_result.sessions
            ),
            "traceability": "selected PointInTimeStore revisions retain provider source IDs",
        },
        "policies": {
            "pit_knowledge_policy": record_result.manifest.pit_knowledge_policy,
            "market_data_price_policy": record_result.manifest.market_data_price_policy,
        },
        "config": {
            **config_json,
            "initial_cash": _decimal_text(_INITIAL_CASH),
            "digest": _sha256("strategy-a-config", config_json),
        },
        "llm": {
            "mode": "recorded-fixture",
            "live_llm_claim": "not-live-llm",
            "model_identity": _MODEL_IDENTITY,
            "model_revision": _MODEL_REVISION,
            "model_identity_policy_id": _MODEL_POLICY_ID,
            "prompt_template_id": _PROMPT_TEMPLATE_ID,
            "prompt_template_digest": _PROMPT_DIGEST,
            "request_fingerprints": [item.request_fingerprint for item in requests],
            "response_digests": [item.response_digest for item in decisions],
            "decision_ids": [item.decision_id for item in decisions],
            "attempt_count": len(attempts),
            "decision_count": len(decisions),
        },
        "lifecycle": {
            "candidate": {
                "session_date": _DECISION_DATE,
                "candidate_id": candidate.candidate_id,
                "symbol": candidate.symbol,
                "regime": candidate.regime.value,
                "data_quality": candidate.data_quality.value,
                "reason_codes": list(candidate.reason_codes),
                "evidence_ids": list(candidate.evidence_ids),
                "action_targets": action_targets,
            },
            "decision": {
                "decision_id": record.decision_id,
                "request_fingerprint": record.request_fingerprint,
                "response_digest": record.response_digest,
                "action": selection.action.value,
                "confidence": selection.confidence,
                "resolved": True,
            },
            "intent": _model_json(buy_intent),
            "risk_decision": _model_json(decision_session.risk_decisions[0]),
            "submitted_order": _model_json(pending),
            "terminal_fill": _model_json(fill),
        },
        "final": {
            "cash": _decimal_text(record_result.final_snapshot.cash),
            "nav": _decimal_text(record_result.final_snapshot.nav),
            "realized_pnl": _decimal_text(record_result.realized_pnl),
            "lots": [_model_json(item) for item in record_result.final_lots],
            "positions": [_model_json(item) for item in record_result.final_snapshot.positions],
        },
        "result_digests": {
            "spec": record_result.spec_fingerprint,
            "resolved_data": record_result.resolved_data_fingerprint,
            "backtest_result": "backtest-result-sha256:" + hashlib.sha256(record_bytes).hexdigest(),
            "intents": "strategy-intents-sha256:" + hashlib.sha256(record_intents).hexdigest(),
        },
        "reproducibility": {
            "journal_close_reopen": True,
            "record_replay_intents_byte_identical": True,
            "record_replay_result_byte_identical": True,
        },
        "attestations": [_summarize_attestation(item) for item in attestations],
        "limitations": {
            "business_availability_not_historical_ingestion_replay": True,
            "raw_unadjusted_prices": True,
            "corporate_actions_not_modeled": True,
            "profitability_not_claimed": True,
            "provider_response_subset_selected_for_bounded_proof": True,
        },
        "tracked_secret_scan": _tracked_secret_scan(repository_root),
    }


def verify_evidence_semantics(evidence: Mapping[str, object], repository_root: Path) -> None:
    try:
        evidence_payload = dict(evidence)
        claimed_evidence_digest = evidence_payload.pop("evidence_digest", None)
        if claimed_evidence_digest != _sha256("strategy-a-evidence", evidence_payload):
            _fail("evidence payload digest is stale")
        if (
            type(evidence) is not dict
            or evidence.get("schema_version") != _SCHEMA_VERSION
            or evidence.get("status") != "success"
        ):
            _fail("evidence schema or status is invalid")
        if evidence.get("relevant_source_digest") != compute_relevant_source_digest(
            repository_root
        ):
            _fail("evidence relevant source digest is stale")
        fixture = evidence["fixture"]
        if (
            type(fixture) is not dict
            or fixture.get("dataset_digest") != STRATEGY_A_SCHEDULE.fixture_dataset_digest
            or fixture.get("row_count") != 5
            or fixture.get("response_total_records") != 13
            or fixture.get("normalized_response_digest") != _NORMALIZED_RESPONSE_DIGEST
            or fixture.get("selection_policy")
            != "minimal-contiguous-window-with-natural-offensive-signal/v1"
            or fixture.get("selection_start") != "2026-07-24"
            or fixture.get("selection_end") != "2026-07-30"
        ):
            _fail("evidence fixture digest, row count, or selection metadata is invalid")
        llm = evidence["llm"]
        if (
            type(llm) is not dict
            or llm.get("mode") != "recorded-fixture"
            or llm.get("live_llm_claim") != "not-live-llm"
        ):
            _fail("evidence falsely claims a live LLM")
        if (
            not llm.get("request_fingerprints")
            or not llm.get("response_digests")
            or not llm.get("decision_ids")
        ):
            _fail("evidence decision digests are empty")
        config = evidence["config"]
        if type(config) is not dict:
            _fail("evidence config is invalid")
        config_payload = {
            name: config.get(name) for name in StrategyAConfig.model_fields
        }
        if config.get("digest") != _sha256("strategy-a-config", config_payload):
            _fail("evidence config digest is stale")
        source = evidence["source_records"]
        expected_ids = sorted(STRATEGY_A_SCHEDULE.expected_source_record_ids)
        if (
            type(source) is not dict
            or source.get("ids") != expected_ids
            or source.get("digest") != _source_record_digest(tuple(expected_ids))
        ):
            _fail("evidence source IDs or digest are stale")
        policies = evidence["policies"]
        if policies != {
            "pit_knowledge_policy": _PIT_POLICY,
            "market_data_price_policy": _PRICE_POLICY,
        }:
            _fail("evidence policy pair is invalid")
        lifecycle = evidence["lifecycle"]
        if (
            type(lifecycle) is not dict
            or lifecycle["candidate"].get("regime") != "OFFENSIVE"
            or lifecycle["decision"].get("action") != "BUY"
            or lifecycle["decision"].get("resolved") is not True
            or lifecycle["intent"].get("side") != "BUY"
            or lifecycle["submitted_order"].get("status") != "PENDING"
            or lifecycle["terminal_fill"].get("status") != "FILLED"
            or lifecycle["terminal_fill"].get("session_date") != _EXECUTION_DATE
        ):
            _fail("evidence BUY/order/fill lifecycle is incomplete")
        decision = lifecycle["decision"]
        intent = lifecycle["intent"]
        risk = lifecycle["risk_decision"]
        pending = lifecycle["submitted_order"]
        fill = lifecycle["terminal_fill"]
        if (
            decision.get("decision_id") not in llm["decision_ids"]
            or decision.get("request_fingerprint") not in llm["request_fingerprints"]
            or decision.get("response_digest") not in llm["response_digests"]
            or decision.get("action") != intent.get("side")
            or risk.get("status") != "APPROVED"
            or risk.get("approved_target_weight") != intent.get("target_weight")
            or pending.get("side") != intent.get("side")
            or pending.get("symbol") != intent.get("symbol")
            or fill.get("order_id") != pending.get("order_id")
            or fill.get("side") != pending.get("side")
            or fill.get("symbol") != pending.get("symbol")
            or fill.get("filled_quantity") != pending.get("requested_quantity")
        ):
            _fail("evidence decision, risk, order, and fill are not cross-linked")
        final = evidence["final"]
        if (
            type(final) is not dict
            or type(config) is not dict
            or not final.get("lots")
            or not final.get("positions")
            or final.get("nav") == config.get("initial_cash")
        ):
            _fail("evidence final portfolio or NAV change is missing")
        reproducibility = evidence["reproducibility"]
        if reproducibility != {
            "journal_close_reopen": True,
            "record_replay_intents_byte_identical": True,
            "record_replay_result_byte_identical": True,
        }:
            _fail("evidence replay equality is false")
        attestations = evidence["attestations"]
        run_at = _parse_utc(evidence["run_at_utc"], "run_at_utc")
        if (
            type(attestations) is not list
            or [item.get("mode") for item in attestations] != ["RECORD", "REPLAY"]
            or not all(item.get("outside_backtest_result") is True for item in attestations)
        ):
            _fail("record/replay attestations are missing")
        attestation_times = [
            _parse_utc(item.get("occurred_at"), "attestation occurred_at")
            for item in attestations
        ]
        if (
            attestation_times != sorted(attestation_times)
            or any(item > run_at for item in attestation_times)
            or any(
                sorted(item.get("decision_ids", [])) != sorted(llm["decision_ids"])
                for item in attestations
            )
        ):
            _fail("attestation chronology or decision references are invalid")
        scan = evidence["tracked_secret_scan"]
        if (
            type(scan) is not dict
            or scan.get("passed") is not True
            or scan.get("finding_count") != 0
        ):
            _fail("tracked-secret scan did not pass")
    except (KeyError, TypeError, AttributeError):
        _fail("evidence structure is incomplete")


def verify_strategy_a_fixture(
    *,
    fixture_path: Path,
    evidence_path: Path,
    repository_root: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    temporary_parent: Path | None = None,
) -> dict[str, object]:
    fixture, rows = _load_fixture(fixture_path)
    run_at = clock()
    if type(run_at) is not datetime or run_at.tzinfo is None:
        _fail("clock must return an aware datetime")
    config = _config()
    transport = _RequestAwareRecordedFixtureTransport()
    with tempfile.TemporaryDirectory(
        prefix="strategy-a-real-data-", dir=temporary_parent
    ) as temporary:
        journal_path = Path(temporary) / "llm-decisions.duckdb"
        record_store, spec = _store_and_spec(rows)
        try:
            with LLMDecisionJournal(journal_path) as journal:
                provider = RecordedLLMDecisionProvider(
                    transport=transport,
                    journal=journal,
                    model_policy=_policy(),
                    invocation_boundary=_DeterministicInvocationBoundary(run_at),
                )
                recorded = _runner(record_store, _strategy(config, provider)).run(spec)
                decisions = journal.list_decisions()
                attempts = journal.list_attempts()
                decision_ids = tuple(sorted(item.decision_id for item in decisions))
                if not decision_ids:
                    _fail("record run produced no canonical decisions")
                journal.append_attestation(
                    LLMRunAttestation(
                        attestation_id="strategy-a-real-data-record",
                        execution_label="Strategy A recorded fixture proof",
                        invocation_mode=LLMInvocationMode.RECORD,
                        decision_ids=decision_ids,
                        occurred_at=run_at.astimezone(UTC) - timedelta(minutes=2),
                    )
                )
        finally:
            record_store.close()

        replay_store, replay_spec = _store_and_spec(rows)
        try:
            with LLMDecisionJournal(journal_path) as reopened:
                if reopened.list_decisions() != decisions or reopened.list_attempts() != attempts:
                    _fail("journal close/reopen did not preserve decisions")
                replay_provider = ReplayLLMDecisionProvider(
                    journal=reopened, model_policy=_policy()
                )
                replayed = _runner(replay_store, _strategy(_config(), replay_provider)).run(
                    replay_spec
                )
                if len(transport.requests) != len(attempts):
                    _fail("replay unexpectedly invoked the recorded transport")
                reopened.append_attestation(
                    LLMRunAttestation(
                        attestation_id="strategy-a-real-data-replay",
                        execution_label="Strategy A fresh replay proof",
                        invocation_mode=LLMInvocationMode.REPLAY,
                        decision_ids=decision_ids,
                        occurred_at=run_at.astimezone(UTC) - timedelta(minutes=1),
                    )
                )
                attestations = reopened.list_attestations()
        finally:
            replay_store.close()

    evidence = _assemble_evidence(
        fixture=fixture,
        record_result=recorded,
        replay_result=replayed,
        requests=tuple(transport.requests),
        decisions=decisions,
        attempts=attempts,
        attestations=attestations,
        repository_root=repository_root,
        run_at=run_at,
    )
    evidence["evidence_digest"] = _sha256("strategy-a-evidence", evidence)
    verify_evidence_semantics(evidence, repository_root)
    atomic_write_evidence(evidence_path, evidence)
    return evidence


def run_cli(
    argv: list[str],
    *,
    repository_root: Path | None = None,
    evidence_path: Path | None = None,
    output: Callable[[str], Any] = print,
) -> int:
    root = Path(__file__).resolve().parents[1] if repository_root is None else repository_root
    target = (
        root / "docs/verification/strategy-a-real-data.json"
        if evidence_path is None
        else evidence_path
    )
    if argv == ["--refresh"]:
        output(
            "Refresh is unsupported: use the committed fixture and offline default; "
            "do not fake a network refresh."
        )
        return 2
    if argv not in ([], ["--schedule", STRATEGY_A_SCHEDULE_ID]):
        output(f"Usage: verify_strategy_a_real_data.py [--schedule {STRATEGY_A_SCHEDULE_ID}]")
        return 2
    try:
        evidence = verify_strategy_a_fixture(
            fixture_path=root / "tests/fixtures/strategy_a/nasdaq-ibm-2026-07.json",
            evidence_path=target,
            repository_root=root,
        )
    except StrategyARealDataVerificationError as error:
        output(json.dumps({"status": "failure", "error": str(error)}, sort_keys=True))
        return 1
    output(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
