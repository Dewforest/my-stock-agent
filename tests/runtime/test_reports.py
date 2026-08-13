from __future__ import annotations

from pathlib import Path

from stock_agent.domain import Market
from stock_agent.runtime.reports import (
    ReportStatus,
    RuntimeReport,
    classify_failure,
    currency_for,
    publish_report,
    report_id_for,
    report_to_json,
    report_to_markdown,
)


def make_report(market: Market, status: ReportStatus = ReportStatus.SUCCEEDED) -> RuntimeReport:
    digests = (
        "run-sha256:" + "a" * 64,
        "config-sha256:" + "b" * 64,
        "universe-sha256:" + "c" * 64,
        "calendar-sha256:" + "d" * 64,
        "source-sha256:" + "e" * 64,
        "decision-sha256:" + "f" * 64,
        "risk-sha256:" + "0" * 64,
        "order-sha256:" + "1" * 64,
        "execution-sha256:" + "2" * 64,
        "ledger-sha256:" + "3" * 64,
    )
    (
        run_id,
        config_digest,
        universe_digest,
        calendar_digest,
        source_digest,
        decision_digest,
        risk_digest,
        order_digest,
        execution_digest,
        ledger_digest,
    ) = digests
    return RuntimeReport(
        report_id=report_id_for(market=market, status=status, run_id=run_id, digests=digests[1:]),
        market=market,
        currency=currency_for(market),
        status=status,
        run_id=run_id,
        config_digest=config_digest,
        universe_digest=universe_digest,
        calendar_digest=calendar_digest,
        source_digest=source_digest,
        decision_digest=decision_digest,
        risk_digest=risk_digest,
        order_digest=order_digest,
        execution_digest=execution_digest,
        ledger_digest=ledger_digest,
    )


def test_report_contains_all_identities() -> None:
    report = make_report(Market.US)
    payload = report_to_json(report)
    for digest in (
        report.run_id,
        report.config_digest,
        report.universe_digest,
        report.calendar_digest,
        report.source_digest,
        report.decision_digest,
        report.risk_digest,
        report.order_digest,
        report.execution_digest,
        report.ledger_digest,
    ):
        assert digest is not None and digest in payload


def test_cny_and_usd_reports_remain_separate() -> None:
    us = make_report(Market.US)
    cn = make_report(Market.CN)
    assert us.currency == "USD"
    assert cn.currency == "CNY"
    assert us.report_id != cn.report_id


def test_rebuild_is_byte_identical() -> None:
    first = make_report(Market.US)
    second = make_report(Market.US)
    assert first.report_id == second.report_id
    assert report_to_json(first) == report_to_json(second)


def test_atomic_publish_leaves_no_partial_file(tmp_path: Path) -> None:
    report = make_report(Market.US)
    target = publish_report(report, tmp_path)
    assert target.exists()
    assert target.read_text(encoding="utf-8") == report_to_json(report)
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_interrupted_publication_repairs(tmp_path: Path) -> None:
    report = make_report(Market.US)
    # A stale partial temp file from a prior interrupted write.
    stale = tmp_path / f".{report.report_id}.12345.tmp"
    stale.write_text("partial", encoding="utf-8")
    target = publish_report(report, tmp_path)
    assert target.read_text(encoding="utf-8") == report_to_json(report)
    assert not stale.exists()


def test_reports_exclude_secret_markers() -> None:
    report = make_report(Market.US)
    payload = report_to_json(report)
    lower = payload.lower()
    for marker in ("authorization", "api-key", "bearer", "password", "token="):
        assert marker not in lower


def test_failure_classification_is_safe() -> None:
    # A traceback-bearing error is reduced to its type name, never locals.
    error = RuntimeError("secret token=abc123 in local variable")
    assert classify_failure(error) == "RuntimeError"
    assert "abc123" not in classify_failure(error)


def test_markdown_derives_from_same_model() -> None:
    report = make_report(Market.CN, status=ReportStatus.MISSED_DECISION_DEADLINE)
    md = report_to_markdown(report)
    assert "CNY" in md
    assert "MISSED_DECISION_DEADLINE" in md
    assert report.report_id in md
