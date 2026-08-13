from __future__ import annotations

from pathlib import Path

import pytest

from stock_agent.runtime.keychain import (
    APPROVED_KEYCHAIN_SERVICES,
    KeychainError,
    KeychainSecretSource,
    Secret,
)

DEEPSEEK = "com.dewforest.my-stock-agent.deepseek"
ALPHA = "com.dewforest.my-stock-agent.alpha-vantage"


def make_runner(secret: str = "opaque-secret-value", returncode: int = 0):
    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(argv: tuple[str, ...], timeout: float) -> tuple[int, bytes, bytes]:
        calls.append((argv, timeout))
        return returncode, secret.encode("utf-8"), b""

    return runner, calls


def test_argv_is_fixed_without_shell() -> None:
    runner, calls = make_runner()
    source = KeychainSecretSource(account="account-a", service=DEEPSEEK, runner=runner, timeout=7.0)
    source.read()
    assert calls[0][0] == (
        "security",
        "find-generic-password",
        "-a",
        "account-a",
        "-s",
        DEEPSEEK,
        "-w",
    )
    assert calls[0][1] == 7.0


def test_only_approved_services_are_addressable() -> None:
    with pytest.raises(KeychainError):
        KeychainSecretSource(account="a", service="com.attacker.evil", runner=make_runner()[0])


def test_secret_is_never_in_repr_str_or_error() -> None:
    secret = Secret("super-secret-token")
    assert "super-secret-token" not in repr(secret)
    assert "super-secret-token" not in str(secret)
    assert secret.expose() == "super-secret-token"


def test_nonzero_exit_raises_stable_error_without_stderr() -> None:
    runner, _ = make_runner(secret="", returncode=1)
    source = KeychainSecretSource(account="a", service=DEEPSEEK, runner=runner)
    with pytest.raises(KeychainError) as info:
        source.read()
    assert "secret" not in str(info.value)


def test_malformed_secret_raises() -> None:
    runner, _ = make_runner(secret="", returncode=0)
    source = KeychainSecretSource(account="a", service=DEEPSEEK, runner=runner)
    with pytest.raises(KeychainError):
        source.read()


def test_construction_does_not_read_invocation_does() -> None:
    runner, calls = make_runner()
    source = KeychainSecretSource(account="a", service=DEEPSEEK, runner=runner)
    assert calls == []
    source.read()
    assert len(calls) == 1


def test_approved_services_are_exactly_deepseek_and_alpha_vantage() -> None:
    assert APPROVED_KEYCHAIN_SERVICES == {DEEPSEEK, ALPHA}


def test_no_allow_any_app_flag_or_enumeration() -> None:
    source_path = (
        Path(__file__).resolve().parents[2] / "src" / "stock_agent" / "runtime" / "keychain.py"
    )
    text = source_path.read_text(encoding="utf-8")
    assert "-A" not in text
    assert "dump-keychain" not in text
    assert "security list-keychains" not in text
