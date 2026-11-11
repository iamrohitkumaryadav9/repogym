"""The startup model-ID check: one models.list call, fail fast on a missing ID, save evidence."""

import json
from types import SimpleNamespace

import pytest

from repogym.experiments.models_check import ModelCheckError, check_models, list_models, verify


def entry(name, version="1", actions=("generateContent",)):
    return SimpleNamespace(
        name=f"models/{name}",
        display_name=name,
        version=version,
        supported_actions=list(actions),
        input_token_limit=1_000_000,
        output_token_limit=65_536,
    )


class FakeModels:
    def __init__(self, listed=(), error=None):
        self.listed, self.error, self.calls = listed, error, 0

    def list(self):
        self.calls += 1
        if self.error:
            raise self.error
        return iter(self.listed)


def fake_client(*names, error=None):
    return SimpleNamespace(models=FakeModels([entry(n) for n in names], error))


IDS = ["gemini-3.5-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it"]


def test_every_configured_model_found_makes_one_list_call_and_returns_evidence():
    client = fake_client(*IDS, "gemini-3.1-flash-lite", "other-model")
    record = check_models("key", IDS, client=client)
    assert client.models.calls == 1  # one models.list call, nothing else on the client
    assert record["models_listed"] == 5 and set(record["configured"]) == set(IDS)
    assert record["configured"]["gemma-4-31b-it"]["name"] == "models/gemma-4-31b-it"
    assert record["method"].endswith("no generation)")


def test_a_missing_model_fails_fast_naming_it_and_the_same_family():
    client = fake_client("gemini-3.5-flash-lite", "gemma-4-26b-a4b-it", "gemma-4-31b")
    with pytest.raises(ModelCheckError, match="gemma-4-31b-it") as info:
        check_models("key", IDS, client=client)
    assert "gemma-4-31b" in str(info.value) and "configs/models.yaml" in str(info.value)


def test_a_failing_list_call_is_a_clear_error_not_a_pass():
    client = fake_client(error=ConnectionError("down"))
    with pytest.raises(ModelCheckError, match="could not fetch the model list: ConnectionError"):
        list_models("key", client)


def test_the_evidence_is_saved_only_for_configured_models(tmp_path):
    check_models("key", IDS, tmp_path / "run", client=fake_client(*IDS, "unrelated"))
    saved = json.loads((tmp_path / "run" / "models_list.json").read_text())
    assert saved["models_listed"] == 4 and "unrelated" not in json.dumps(saved["configured"])


def test_verify_accepts_names_with_or_without_the_models_prefix():
    available = [
        {"name": "models/gemma-4-31b-it"},
        {"name": "gemini-3.5-flash-lite"},
    ]
    assert set(verify(["gemma-4-31b-it", "gemini-3.5-flash-lite"], available)["configured"]) == {
        "gemma-4-31b-it",
        "gemini-3.5-flash-lite",
    }
