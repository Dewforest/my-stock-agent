from __future__ import annotations

from collections.abc import Callable, Mapping
from types import CellType, FrameType, FunctionType, MethodType, TracebackType
from typing import Any

from stock_agent.data.providers import MarketDataError


def capture_market_data_error(call: Callable[[], object]) -> MarketDataError:
    """Catch at a disposable boundary so the user's caller frame is not in the traceback."""
    caught: MarketDataError | None = None
    try:
        call()
    except MarketDataError as error:
        caught = error
    finally:
        call = None  # type: ignore[assignment]
    if caught is None:
        raise AssertionError("expected MarketDataError")
    return caught


def assert_exception_graph_excludes(
    error: MarketDataError,
    canaries: tuple[str | bytes, ...],
) -> None:
    """Walk the exception graph and every provider-owned traceback frame."""
    seen: set[int] = set()
    pending: list[object] = [error]

    def assert_clean(value: object) -> None:
        rendered: list[str | bytes] = []
        if isinstance(value, (str, bytes)):
            rendered.append(value)
        for renderer in (str, repr):
            try:
                rendered.append(renderer(value))
            except BaseException:
                pass
        for canary in canaries:
            for candidate in rendered:
                if isinstance(candidate, bytes):
                    needle = canary if isinstance(canary, bytes) else canary.encode()
                    assert needle not in candidate, f"canary leaked through {type(value)!r}"
                else:
                    needle = (
                        canary.decode(errors="replace")
                        if isinstance(canary, bytes)
                        else canary
                    )
                    assert needle not in candidate, f"canary leaked through {type(value)!r}"

    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        assert_clean(value)

        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend(
                (
                    value.__cause__,
                    value.__context__,
                    value.__traceback__,
                    value.__suppress_context__,
                )
            )
        if isinstance(value, TracebackType):
            pending.append(value.tb_next)
            frame = value.tb_frame
            module_name = frame.f_globals.get("__name__")
            if (
                type(module_name) is str
                and module_name.startswith("stock_agent.data.providers")
            ):
                assert_clean(frame)
                pending.extend(frame.f_locals.values())
            continue
        if isinstance(value, FrameType):
            module_name = value.f_globals.get("__name__")
            if (
                type(module_name) is str
                and module_name.startswith("stock_agent.data.providers")
            ):
                pending.extend(value.f_locals.values())
            continue
        if isinstance(value, CellType):
            try:
                pending.append(value.cell_contents)
            except ValueError:
                pass
        elif isinstance(value, MethodType):
            pending.extend((value.__func__, value.__self__))
        elif isinstance(value, FunctionType):
            if value.__closure__ is not None:
                pending.extend(value.__closure__)
        if isinstance(value, Mapping):
            for key, item in value.items():
                pending.extend((key, item))
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)

        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, Mapping):
            pending.extend(attributes.values())
        for owner in type(value).__mro__:
            slots: Any = owner.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in {"__dict__", "__weakref__"}:
                    continue
                try:
                    pending.append(getattr(value, slot))
                except BaseException:
                    pass
