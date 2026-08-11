from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.verify_strategy_a_real_data as verifier
from scripts.strategy_a_live_schedule import STRATEGY_A_SCHEDULE, STRATEGY_A_SCHEDULE_ID

FIXTURE = Path("tests/fixtures/strategy_a/nasdaq-ibm-2026-07.json")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Strategy A fixture verification must remain offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "fixture.json"
    target.write_bytes(FIXTURE.read_bytes())
    return target


def _mutate_fixture(tmp_path: Path, mutate: object) -> Path:
    target = _copy_fixture(tmp_path)
    payload = json.loads(target.read_text())
    mutate(payload)  # type: ignore[operator]
    target.write_text(json.dumps(payload))
    return target


def _change_row_and_refresh_dataset_digest(payload: dict[str, object]) -> None:
    rows = payload["rows"]
    assert isinstance(rows, list)
    rows[2]["close"] = "227.56"
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["dataset_digest"] = "dataset-sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def test_frozen_schedule_is_exactly_the_five_provider_sessions() -> None:
    assert STRATEGY_A_SCHEDULE_ID == "strategy-a-us-ibm-2026-07-provider-fixture-v1"
    assert tuple(row.session_date.isoformat() for row in STRATEGY_A_SCHEDULE.schedule.sessions) == (
        "2026-07-24",
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
        "2026-07-30",
    )
    assert all(row.timezone == "America/New_York" for row in STRATEGY_A_SCHEDULE.schedule.sessions)
    assert STRATEGY_A_SCHEDULE.fixture_dataset_digest == (
        "dataset-sha256:5183efd941801b2aacba9fcf7aa8a01425a79845218f5c8bfad0ef62e83128f6"
    )


def test_offline_record_close_reopen_and_fresh_replay_produce_trade_evidence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "evidence.json"
    evidence = verifier.verify_strategy_a_fixture(
        fixture_path=FIXTURE,
        evidence_path=output,
        repository_root=Path.cwd(),
        clock=lambda: datetime(2026, 8, 6, 12, tzinfo=UTC),
        temporary_parent=tmp_path,
    )

    assert evidence == json.loads(output.read_text())
    assert evidence["schema_version"] == "strategy-a-real-data-verification/v1"
    assert evidence["status"] == "success"
    assert evidence["run_at_utc"] == "2026-08-06T12:00:00Z"
    assert evidence["llm"]["mode"] == "recorded-fixture"
    assert evidence["llm"]["live_llm_claim"] == "not-live-llm"
    assert evidence["policies"] == {
        "pit_knowledge_policy": "current-view-baseline/v1",
        "market_data_price_policy": "raw-unadjusted/no-corporate-actions/v1",
    }
    assert evidence["fixture"]["dataset_digest"] == STRATEGY_A_SCHEDULE.fixture_dataset_digest
    assert evidence["fixture"]["response_total_records"] == 13
    assert evidence["fixture"]["selection_policy"] == (
        "minimal-contiguous-window-with-natural-offensive-signal/v1"
    )
    assert evidence["lifecycle"]["candidate"]["regime"] == "OFFENSIVE"
    assert evidence["lifecycle"]["decision"]["action"] == "BUY"
    assert evidence["lifecycle"]["intent"]["side"] == "BUY"
    assert evidence["lifecycle"]["risk_decision"]["status"] in {"APPROVED", "CLAMPED"}
    assert evidence["lifecycle"]["submitted_order"]["status"] == "PENDING"
    assert evidence["lifecycle"]["terminal_fill"]["status"] == "FILLED"
    assert evidence["lifecycle"]["terminal_fill"]["session_date"] == "2026-07-29"
    assert evidence["final"]["cash"] != evidence["config"]["initial_cash"]
    assert evidence["final"]["nav"] != evidence["config"]["initial_cash"]
    assert evidence["final"]["lots"] and evidence["final"]["positions"]
    assert evidence["reproducibility"] == {
        "journal_close_reopen": True,
        "record_replay_intents_byte_identical": True,
        "record_replay_result_byte_identical": True,
    }
    assert [item["mode"] for item in evidence["attestations"]] == ["RECORD", "REPLAY"]
    assert evidence["tracked_secret_scan"]["passed"] is True
    verifier.verify_evidence_semantics(evidence, Path.cwd())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(extra="forbidden"),
        lambda payload: payload["rows"][2].update(close="227.56"),
        lambda payload: payload["rows"][2].update(
            source_record_id="provider-payload-sha256:" + "0" * 64
        ),
        lambda payload: payload["rows"][2].update(volume=13262960),
        lambda payload: payload["rows"].reverse(),
        lambda payload: payload["response_rows"][0].update(close="$235.93"),
        _change_row_and_refresh_dataset_digest,
    ],
)
def test_fixture_mutations_fail_closed_without_writing_evidence(
    tmp_path: Path, mutation: object
) -> None:
    fixture = _mutate_fixture(tmp_path, mutation)
    output = tmp_path / "must-not-exist.json"

    with pytest.raises(verifier.StrategyARealDataVerificationError):
        verifier.verify_strategy_a_fixture(
            fixture_path=fixture,
            evidence_path=output,
            repository_root=Path.cwd(),
            clock=lambda: datetime(2026, 8, 6, 12, tzinfo=UTC),
            temporary_parent=tmp_path,
        )

    assert not output.exists()


def test_secret_scan_includes_untracked_nonignored_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.txt").write_bytes(b"sk-" + b"proj-" + b"fixture")

    with pytest.raises(
        verifier.StrategyARealDataVerificationError,
        match="credential-shaped content",
    ):
        verifier._tracked_secret_scan(tmp_path)


def test_evidence_mutations_fail_semantic_revalidation(tmp_path: Path) -> None:
    evidence = verifier.verify_strategy_a_fixture(
        fixture_path=FIXTURE,
        evidence_path=tmp_path / "valid.json",
        repository_root=Path.cwd(),
        clock=lambda: datetime(2026, 8, 6, 12, tzinfo=UTC),
        temporary_parent=tmp_path,
    )
    mutations = (
        lambda item: item["llm"].update(live_llm_claim="live-llm"),
        lambda item: item.update(relevant_source_digest="relevant-source-sha256:" + "0" * 64),
        lambda item: item["source_records"]["ids"].pop(),
        lambda item: item["reproducibility"].update(record_replay_result_byte_identical=False),
        lambda item: item["lifecycle"]["decision"].update(action="HOLD"),
        lambda item: item["fixture"].update(selection_policy="all-provider-records/v1"),
        lambda item: item["config"].update(digest="strategy-a-config-sha256:" + "0" * 64),
        lambda item: item["result_digests"].update(
            backtest_result="backtest-result-sha256:" + "0" * 64
        ),
        lambda item: item["lifecycle"]["decision"].update(
            decision_id="llm-decision-sha256:" + "0" * 64
        ),
        lambda item: item["lifecycle"]["risk_decision"].update(status="REJECTED"),
        lambda item: item["final"].update(cash="1"),
    )
    for mutation in mutations:
        changed = copy.deepcopy(evidence)
        mutation(changed)
        with pytest.raises(verifier.StrategyARealDataVerificationError):
            verifier.verify_evidence_semantics(changed, Path.cwd())


def test_refresh_mode_is_explicitly_unsupported_and_does_not_fake_network(tmp_path: Path) -> None:
    messages: list[str] = []
    exit_code = verifier.run_cli(
        ["--refresh"],
        repository_root=Path.cwd(),
        evidence_path=tmp_path / "must-not-exist.json",
        output=messages.append,
    )
    assert exit_code == 2
    assert "unsupported" in messages[0].lower()
    assert "offline" in messages[0].lower()
    assert not (tmp_path / "must-not-exist.json").exists()
