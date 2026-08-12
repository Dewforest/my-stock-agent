from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from stock_agent.domain import Market
from stock_agent.runtime.calendars import (
    AuthorityStatus,
    EvidenceClass,
    Exchange,
    ExchangeSchedule,
    PrivateInstallProfile,
    ScheduleProvenance,
    SourceArtifact,
    TradingSession,
    canonical_schedule_digest,
)


class PrivateCalendarGenerationError(RuntimeError):
    """Stable, sanitized failure at the private calendar generation boundary."""


def _private_calendar_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "my-stock-agent" / "calendars"


def _load_source(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PrivateCalendarGenerationError("invalid_source_artifact") from None
    if type(payload) is not dict:
        raise PrivateCalendarGenerationError("invalid_source_artifact")
    return payload


def _build_schedule(
    payload: dict[str, Any], profile: PrivateInstallProfile
) -> tuple[SourceArtifact, ExchangeSchedule]:
    try:
        artifact = SourceArtifact.model_validate_json(
            json.dumps(payload["source_artifact"]), strict=True
        )
        raw_sessions = payload["sessions"]
        if type(raw_sessions) is not list:
            raise TypeError("sessions must be a list")
        sessions = tuple(
            TradingSession.model_validate_json(json.dumps(item), strict=True)
            for item in raw_sessions
        )
        closures = TypeAdapter(tuple[date, ...]).validate_json(
            json.dumps(payload["closures"]), strict=True
        )
        coverage_from = TypeAdapter(date).validate_json(
            json.dumps(payload["coverage_from"]), strict=True
        )
        coverage_through = TypeAdapter(date).validate_json(
            json.dumps(payload["coverage_through"]), strict=True
        )
        schedule_id = payload["schedule_id"]
        timezone = payload["timezone"]
        year = payload["year"]
        if type(schedule_id) is not str or type(timezone) is not str or type(year) is not int:
            raise TypeError("schedule scalar types are invalid")
        schedule_values = {
            "schedule_id": schedule_id,
            "schedule_digest": "schedule-sha256:" + "0" * 64,
            "exchange": Exchange(payload["exchange"]),
            "market": Market(payload["market"]),
            "timezone": timezone,
            "year": year,
            "coverage_from": coverage_from,
            "coverage_through": coverage_through,
            "provenance": ScheduleProvenance(
                source_id=artifact.source_id,
                source_digest=f"source-sha256:{artifact.raw_sha256}",
                parser_id="private-runtime-calendar-json/v1",
                license_profile_id=profile.profile_id,
                install_profile_id=profile.profile_id,
                evidence_class=EvidenceClass.RESEARCH_REPORT,
                source_artifact_approved=False,
                license_approved=profile.license_approved,
                install_approved=profile.install_approved,
                authority=AuthorityStatus.PARTIAL,
            ),
            "sessions": sessions,
            "closures": closures,
        }
        provisional = ExchangeSchedule.model_construct(**schedule_values)
        digest = canonical_schedule_digest(provisional)
        schedule = ExchangeSchedule.model_validate(
            {**schedule_values, "schedule_digest": digest}, strict=True
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise PrivateCalendarGenerationError("invalid_source_artifact") from None
    return artifact, schedule


def _open_private_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise PrivateCalendarGenerationError("unsafe_output_directory")
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    current_descriptor = os.open("/", flags)
    user_owned_anchor_seen = False
    try:
        for component in absolute.parts[1:]:
            next_descriptor = -1
            try:
                try:
                    next_descriptor = os.open(component, flags, dir_fd=current_descriptor)
                except FileNotFoundError:
                    if not user_owned_anchor_seen:
                        raise PrivateCalendarGenerationError("unsafe_output_directory") from None
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_descriptor)
                        next_descriptor = os.open(component, flags, dir_fd=current_descriptor)
                    except OSError:
                        raise PrivateCalendarGenerationError("unsafe_output_directory") from None
                except OSError:
                    raise PrivateCalendarGenerationError("unsafe_output_directory") from None
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise PrivateCalendarGenerationError("unsafe_output_directory")
                if metadata.st_uid == os.getuid():
                    user_owned_anchor_seen = True
                elif user_owned_anchor_seen:
                    raise PrivateCalendarGenerationError("unsafe_output_directory")
                os.close(current_descriptor)
                current_descriptor = next_descriptor
                next_descriptor = -1
            finally:
                if next_descriptor >= 0:
                    os.close(next_descriptor)
        if not user_owned_anchor_seen:
            raise PrivateCalendarGenerationError("unsafe_output_directory")
        os.fchmod(current_descriptor, 0o700)
        return current_descriptor
    except BaseException:
        os.close(current_descriptor)
        raise


def _atomic_private_write(*, directory_descriptor: int, target_name: str, payload: str) -> None:
    temporary_name = f".calendar-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        raise


def generate_private_calendar(
    *,
    source_path: Path,
    profile: PrivateInstallProfile,
) -> Path:
    if not profile.approved:
        raise PrivateCalendarGenerationError("profile_not_approved")
    artifact, schedule = _build_schedule(_load_source(source_path), profile)
    output_directory = _private_calendar_root()
    directory_descriptor = _open_private_directory(output_directory)
    target_name = f"{schedule.exchange.value.lower()}-{schedule.year}.json"
    target = output_directory / target_name
    envelope = {
        "schema": "private-runtime-calendar/v1",
        "source_artifact": artifact.model_dump(mode="json"),
        "schedule": schedule.model_dump(mode="json"),
    }
    try:
        _atomic_private_write(
            directory_descriptor=directory_descriptor,
            target_name=target_name,
            payload=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        )
    finally:
        os.close(directory_descriptor)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--license-approved", action="store_true")
    parser.add_argument("--install-approved", action="store_true")
    arguments = parser.parse_args()
    profile = PrivateInstallProfile(
        profile_id=arguments.profile_id,
        license_approved=arguments.license_approved,
        install_approved=arguments.install_approved,
    )
    try:
        output = generate_private_calendar(
            source_path=arguments.source,
            profile=profile,
        )
    except PrivateCalendarGenerationError as error:
        print(str(error))
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
