"""Check repogym.openenv_adapter against openenv-core's own models (run locally, not in CI).

    uvx --python 3.11 --from openenv-core==0.3.0 --with-editable . \
        python scripts/openenv_conformance.py

Each adapter model must have exactly OpenEnv's field set and round-trip through OpenEnv's model
in both directions. ToolErrorType values must be identical, reset/step must take OpenEnv's
parameters, and state must be a property.
"""

import inspect

import openenv.core.env_server as oe
from openenv.core.env_server import types as ot

import repogym.openenv_adapter as ra

PAIRS = [
    (ra.ListToolsAction(), oe.ListToolsAction),
    (ra.CallToolAction(tool_name="bash", arguments={"command": "ls"}), oe.CallToolAction),
    (ra.Observation(metadata={"prompt": "p"}), ot.Observation),
    (ra.ListToolsObservation(tools=ra.tools()), oe.ListToolsObservation),
    (
        ra.CallToolObservation(tool_name="bash", result="ok", done=True, reward=1),
        oe.CallToolObservation,
    ),
    (
        ra.CallToolObservation(
            tool_name="x", error=ra.ToolError(error_type="tool_not_found", message="m")
        ),
        oe.CallToolObservation,
    ),
    (ra.State(episode_id="e", step_count=3, task_id="t"), ot.State),
]


def main() -> None:
    for ours, theirs in PAIRS:
        name = type(ours).__name__
        data = ours.model_dump(mode="json")
        back = theirs.model_validate(data)  # ours -> OpenEnv
        again = type(ours).model_validate(back.model_dump(mode="json"))  # OpenEnv -> ours
        assert again.model_dump(mode="json") == data, name
        fields_ours, fields_theirs = set(type(ours).model_fields), set(theirs.model_fields)
        assert fields_ours == fields_theirs, (name, fields_ours ^ fields_theirs)
        print(f"ok  {name:22} <-> openenv {theirs.__name__}")
    assert [e.value for e in ra.ToolErrorType] == [e.value for e in oe.ToolErrorType]
    base = oe.Environment
    for method in ("reset", "step"):
        ours = list(inspect.signature(getattr(ra.RepoGymOpenEnv, method)).parameters)
        theirs = list(inspect.signature(getattr(base, method)).parameters)
        assert set(theirs) <= set(ours), (method, ours, theirs)
        print(f"ok  {method}{tuple(ours[1:])} covers openenv {tuple(theirs[1:])}")
    assert isinstance(inspect.getattr_static(ra.RepoGymOpenEnv, "state"), property)
    print("ok  ToolErrorType values identical; state is a property")


if __name__ == "__main__":
    main()
