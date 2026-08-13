from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Annotated

from pydantic import StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

APPROVED_KEYCHAIN_SERVICES = frozenset(
    {
        "com.dewforest.my-stock-agent.deepseek",
        "com.dewforest.my-stock-agent.alpha-vantage",
    }
)

_SECURITY_ARGV_PREFIX = ("security", "find-generic-password")

# A subprocess runner: argv (without shell) -> (returncode, stdout, stderr).
Runner = Callable[[tuple[str, ...], float], tuple[int, bytes, bytes]]


class KeychainError(Exception):
    """Stable, secret-free failure of a Keychain read."""


class Secret:
    """An opaque, short-lived secret that never reveals its value in repr/str."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if type(value) is not str or not value:
            raise KeychainError("secret must be a nonblank string")
        self._value = value

    def __repr__(self) -> str:
        return "<Secret [REDACTED]>"

    def __str__(self) -> str:
        return "[REDACTED]"

    def expose(self) -> str:
        return self._value


class KeychainSecretSource:
    """Reads exactly one approved Keychain item at invocation time.

    The argument vector is fixed (`security find-generic-password -a <account>
    -s <service> -w`), run without a shell, with a bounded timeout. Only
    approved DeepSeek and Alpha Vantage services are addressable; stdout is
    held in a bounded private variable and never reaches repr/str/argv/error.
    """

    def __init__(
        self,
        *,
        account: str,
        service: str,
        runner: Runner | None = None,
        timeout: float = 5.0,
    ) -> None:
        if type(account) is not str or not account:
            raise KeychainError("account must be a nonblank string")
        if type(service) is not str or not service:
            raise KeychainError("service must be a nonblank string")
        if service not in APPROVED_KEYCHAIN_SERVICES:
            raise KeychainError("service is not an approved Keychain item")
        if timeout <= 0:
            raise KeychainError("timeout must be positive")
        self._account = account
        self._service = service
        self._runner = runner if runner is not None else _default_runner
        self._timeout = timeout

    @property
    def argv(self) -> tuple[str, ...]:
        return (*_SECURITY_ARGV_PREFIX, "-a", self._account, "-s", self._service, "-w")

    def read(self) -> Secret:
        returncode, stdout, _stderr = self._runner(self.argv, self._timeout)
        if returncode != 0:
            raise KeychainError("keychain read failed")
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise KeychainError("keychain secret is malformed") from None
        secret_value = text.strip()
        if not secret_value:
            raise KeychainError("keychain secret is malformed")
        return Secret(secret_value)


def _default_runner(argv: tuple[str, ...], timeout: float) -> tuple[int, bytes, bytes]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr
