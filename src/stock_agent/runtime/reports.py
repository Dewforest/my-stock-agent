from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import StringConstraints

from stock_agent.audit.canonical import tagged_sha256
from stock_agent.domain import Market
from stock_agent.runtime.models import RuntimeModel

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ReportStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    MISSED_DECISION_DEADLINE = "MISSED_DECISION_DEADLINE"
    BLOCKED_KILL_SWITCH = "BLOCKED_KILL_SWITCH"
    SKIPPED_NOT_SESSION = "SKIPPED_NOT_SESSION"
    SKIPPED_TOO_EARLY = "SKIPPED_TOO_EARLY"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class RuntimeReport(RuntimeModel):
    report_id: NonEmptyStr
    market: Market
    currency: NonEmptyStr
    status: ReportStatus
    run_id: str | None = None
    config_digest: str | None = None
    universe_digest: str | None = None
    calendar_digest: str | None = None
    source_digest: str | None = None
    decision_digest: str | None = None
    risk_digest: str | None = None
    order_digest: str | None = None
    execution_digest: str | None = None
    ledger_digest: str | None = None
    error: str | None = None


def currency_for(market: Market) -> str:
    return "CNY" if market is Market.CN else "USD"


def report_id_for(
    *,
    market: Market,
    status: ReportStatus,
    run_id: str | None,
    digests: tuple[str | None, ...],
) -> str:
    currency = currency_for(market)
    return tagged_sha256(
        "runtime-report",
        (market.value, currency, status.value, run_id, *digests),
    )


def report_to_json(report: RuntimeReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def report_to_markdown(report: RuntimeReport) -> str:
    lines = [
        f"# {report.market.value} {report.currency} report",
        "",
        f"- status: `{report.status.value}`",
        f"- report id: `{report.report_id}`",
    ]
    identity_pairs = (
        ("run", report.run_id),
        ("config", report.config_digest),
        ("universe", report.universe_digest),
        ("calendar", report.calendar_digest),
        ("source", report.source_digest),
        ("decision", report.decision_digest),
        ("risk", report.risk_digest),
        ("order", report.order_digest),
        ("execution", report.execution_digest),
        ("ledger", report.ledger_digest),
    )
    for label, digest in identity_pairs:
        if digest is not None:
            lines.append(f"- {label}: `{digest}`")
    if report.error is not None:
        lines.append(f"- error: `{report.error}`")
    return "\n".join(lines) + "\n"


def publish_report(report: RuntimeReport, directory: Path) -> Path:
    if not isinstance(directory, Path):
        raise TypeError("directory must be a Path")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report.report_id}.json"
    content = report_to_json(report)

    if target.exists() and target.read_text(encoding="utf-8") == content:
        return target

    tmp = directory / f".{report.report_id}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    _cleanup_stale_tmp(directory, report.report_id)
    return target


def _cleanup_stale_tmp(directory: Path, report_id: str) -> None:
    for stale in directory.glob(f".{report_id}.*.tmp"):
        stale.unlink(missing_ok=True)


def classify_failure(error: BaseException | None) -> str:
    if error is None:
        return ""
    return type(error).__name__
