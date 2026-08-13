from __future__ import annotations

import json
from collections.abc import Callable

import typer

EXIT_NO_WORK = 0
EXIT_RETRYABLE_FAILURE = 1
EXIT_RECONCILIATION = 2
EXIT_KILL_SWITCH = 3
EXIT_INTERNAL_CORRUPTION = 4

CommandHandler = Callable[[], int]


def emit_envelope(outcome: str, **fields: object) -> None:
    """Emit exactly one bounded, secret-free result envelope to stdout."""
    print(json.dumps({"outcome": outcome, **fields}, sort_keys=True))


def build_app(handlers: dict[str, CommandHandler] | None = None) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, add_completion=False)
    resolved = handlers or {}

    def _invoke(name: str, default: int = EXIT_NO_WORK) -> None:
        handler = resolved.get(name)
        code = default if handler is None else handler()
        raise typer.Exit(code)

    @app.command()
    def run_once() -> None:
        _invoke("run_once")

    @app.command()
    def status() -> None:
        _invoke("status")

    @app.command()
    def report() -> None:
        _invoke("report")

    @app.command()
    def dry_run() -> None:
        _invoke("dry_run")

    @app.command()
    def pause() -> None:
        _invoke("pause")

    @app.command()
    def resume() -> None:
        _invoke("resume")

    return app


app = build_app()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
