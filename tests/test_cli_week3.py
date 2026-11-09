"""Week 3 CLI wiring: test-split refusals that need no Docker or API access."""

import pytest
from typer.testing import CliRunner

from repogym import cli as cli_mod
from repogym.cli import app
from repogym.experiments import runner as runner_mod

cli = CliRunner()


@pytest.fixture(autouse=True)
def keep_logging(monkeypatch):
    """The CLI configures structlog on CliRunner's temporary stdout, which is closed after the
    call; later tests would then log to a closed file. Leave the global config alone."""
    monkeypatch.setattr(cli_mod, "configure_logging", lambda verbose=False: None)


def test_run_needs_an_agent_or_an_experiment():
    result = cli.invoke(app, ["run"])
    assert result.exit_code == 1 and "--agent or --experiment" in result.output


def test_the_llm_baseline_is_refused_on_test_tasks():
    for args in (["--split", "test"], ["--tasks", "python-attrs__attrs-1457"]):
        result = cli.invoke(app, ["run", "--agent", "baseline", *args])
        assert result.exit_code == 2 and "REFUSED" in result.output


def test_gemini_api_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setattr(runner_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GEMINI_API_KEY", " test-key-123 ")
    assert runner_mod.gemini_api_key() == "test-key-123"
    monkeypatch.delenv("GEMINI_API_KEY")
    try:
        runner_mod.gemini_api_key()
    except RuntimeError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:
        raise AssertionError("expected a RuntimeError")
