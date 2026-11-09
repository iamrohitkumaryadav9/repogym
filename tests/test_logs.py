"""Secrets never reach log output."""

import io

import structlog

from repogym.logs import REDACTED, redact, redact_processor


def test_environment_secrets_and_known_shapes_are_redacted(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-secret-value-1234567890")
    text = redact(
        "key=sk-test-secret-value-1234567890 and AIza" + "B" * 35 + " and ya29." + "c" * 30
    )
    assert "sk-test-secret" not in text and "AIza" not in text and "ya29." not in text
    assert text.count(REDACTED) == 3


def test_processor_redacts_nested_fields_and_exceptions(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "tok-0123456789abcdef")
    event = redact_processor(
        None,
        "info",
        {
            "event": "call",
            "headers": {"x-goog-api-key": "tok-0123456789abcdef"},
            "args": ["tok-0123456789abcdef"],
            "error": ValueError("bad key tok-0123456789abcdef"),
            "count": 3,
        },
    )
    assert "tok-0123456789abcdef" not in repr(event) and event["count"] == 3


def test_configured_logger_output_never_contains_the_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-looking-key-ABCDEFGH12345")
    out = io.StringIO()
    structlog.configure(
        processors=[redact_processor, structlog.dev.ConsoleRenderer(colors=False)],
        logger_factory=structlog.PrintLoggerFactory(file=out),
    )
    structlog.get_logger().warning(
        "llm_error", detail="request failed for real-looking-key-ABCDEFGH12345"
    )
    assert "real-looking-key" not in out.getvalue() and REDACTED in out.getvalue()
    structlog.reset_defaults()


def test_short_or_unrelated_values_are_left_alone(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc")  # too short to be a credential
    assert redact("abc and model gemma-4-31b-it") == "abc and model gemma-4-31b-it"
