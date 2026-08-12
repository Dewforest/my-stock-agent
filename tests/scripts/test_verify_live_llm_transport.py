from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

from stock_agent.strategies.llm_provider import RawLLMResponse
from stock_agent.strategies.openai_compatible import OpenAICompatibleResponseError

SCRIPT = Path(__file__).parents[2] / "scripts" / "verify_live_llm_transport.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_live_llm_transport", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(*extra: str) -> list[str]:
    return [
        "--provider",
        "openai",
        "--model",
        "gpt-test",
        "--env-var",
        "OPENAI_TEST_KEY",
        "--max-tokens",
        "128",
        *extra,
    ]


def test_parser_requires_explicit_nonsecret_configuration() -> None:
    module = _module()
    parser = module.build_parser()
    for missing in ("provider", "model", "env-var", "max-tokens"):
        arguments = _args()
        option = f"--{missing}"
        index = arguments.index(option)
        del arguments[index : index + 2]
        with pytest.raises(SystemExit):
            parser.parse_args(arguments)


def test_key_cannot_be_supplied_as_cli_option() -> None:
    parser = _module().build_parser()
    help_text = parser.format_help()
    assert "--api-key" not in help_text
    assert "--key" not in help_text
    with pytest.raises(SystemExit):
        parser.parse_args([*_args(), "--api-key", "forbidden-secret"])


def test_rejected_cli_secret_is_not_echoed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "CAP_CLI_SECRET_f9a71"

    with pytest.raises(SystemExit):
        _module().main([*_args(), "--api-key", canary])

    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert "live LLM transport argument error" in captured.err


def test_help_construction_and_dry_run_never_read_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()

    def forbidden(name: str) -> str:
        raise AssertionError(f"environment read forbidden during dry path: {name}")

    monkeypatch.setattr(os, "getenv", forbidden)
    assert "--execute" in module.build_parser().format_help()
    transport = module.build_transport(module.build_parser().parse_args(_args()))
    assert "OPENAI_TEST_KEY" in repr(transport.http_client._token_source)
    assert module.main(_args()) == 0
    output = capsys.readouterr().out
    assert output == (
        '{"execute":false,"max_tokens":128,"model":"gpt-test",'
        '"provider":"openai","status":"ready"}\n'
    )


def test_deepseek_dry_run_uses_fixed_profile_and_secret_free_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    monkeypatch.setattr(os, "getenv", lambda name: (_ for _ in ()).throw(AssertionError(name)))
    arguments = [
        "--provider",
        "deepseek",
        "--model",
        "deepseek-chat",
        "--env-var",
        "DEEPSEEK_TEST_KEY",
        "--max-tokens",
        "64",
    ]
    assert module.main(arguments) == 0
    output = capsys.readouterr().out
    assert "DEEPSEEK_TEST_KEY" not in output
    assert '"provider":"deepseek"' in output
    transport = module.build_transport(module.build_parser().parse_args(arguments))
    assert transport.profile.host == "api.deepseek.com"
    assert transport.profile.target == "/chat/completions"


def test_execute_with_missing_credential_fails_before_network_with_fixed_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(os, "getenv", lambda name: None)

    assert _module().main(_args("--execute")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "live LLM transport verification failed: http_credential\n"
    assert "OPENAI_TEST_KEY" not in captured.err


def test_execute_reports_only_safe_provider_response_category(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()

    class MalformedProviderTransport:
        def invoke(self, request: object) -> object:
            raise OpenAICompatibleResponseError

    monkeypatch.setattr(module, "build_transport", lambda arguments: MalformedProviderTransport())

    assert module.main(_args("--execute")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "live LLM transport verification failed: provider_response\n"


def test_identity_failure_reports_bounded_returned_model_and_accepts_explicit_expectation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()

    class ReturnedModelTransport:
        def invoke(self, request: object) -> RawLLMResponse:
            return RawLLMResponse(
                payload={},
                model_identity="deepseek-v3.1-terminus",
                model_revision="api-model-id:deepseek-v3.1-terminus",
            )

    monkeypatch.setattr(module, "build_transport", lambda arguments: ReturnedModelTransport())

    assert module.main(_args("--execute")) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "live LLM transport verification failed: identity_policy "
        'returned_model="deepseek-v3.1-terminus"\n'
    )

    parsed = module.build_parser().parse_args(
        [*_args(), "--expected-returned-model", "deepseek-v3.1-terminus"]
    )
    assert parsed.expected_returned_model == "deepseek-v3.1-terminus"
