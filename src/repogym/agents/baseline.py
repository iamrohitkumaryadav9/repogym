"""A minimal bash-only baseline agent, in the style of mini-swe-agent.

Each turn the model gives its reasoning and exactly one ```bash block; that one command runs and
its output becomes the next user message. The linear message history is the trajectory. The
agent finishes by running the sentinel command. Same budgets and grading as every other agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from repogym.agents.models import ChatModel, ModelResponseError, ModelUnavailable, from_config
from repogym.env.environment import Action, Environment, Observation

SENTINEL = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_BLOCK = re.compile(r"```(?:bash|sh)?[ \t]*\n(.*?)```", re.DOTALL)

SYSTEM = f"""You are a software engineer fixing an issue in a Python repository.

Work in a loop. In every reply, first explain your reasoning briefly, then give EXACTLY ONE
bash code block with ONE command (you may chain with && or ;). It runs in /work, the repository
root, with no network access. You'll get its exit code and output.

Rules:
- Find the relevant code, reproduce the issue if you can, and change the source to fix it.
- Don't edit tests to make them pass: test changes are discarded before grading.
- Edit files with standard tools, e.g. sed, or python -c / a heredoc that writes the file.
- When you are done, reply with exactly this command in the block, and nothing else:
  {SENTINEL}

Format:
THOUGHT: <your reasoning>

```bash
<one command>
```"""


def parse_command(text: str) -> tuple[str | None, str | None]:
    """(command, None), or (None, a format-error message for the model)."""
    blocks = [b.strip() for b in _BLOCK.findall(text)]
    if len(blocks) != 1:
        return None, (
            f"Format error: your reply must contain exactly one ```bash block; it had "
            f"{len(blocks)}. Reply with THOUGHT, then one bash block."
        )
    if not blocks[0]:
        return None, "Format error: the bash block is empty."
    return blocks[0], None


def render(observation: Observation) -> str:
    return f"<output>\n{observation.text}\n</output>"


@dataclass
class BaselineAgent:
    model: ChatModel
    name: str = "repogym-baseline-bash"
    version: str = "1.0"

    @property
    def model_name(self) -> str:
        return self.model.name

    def run(self, env: Environment, first: Observation) -> None:
        turns: list[tuple[str, str]] = [("user", first.text)]
        while True:
            try:
                reply = self.model.chat(SYSTEM, turns)
            except ModelResponseError as exc:
                # A blocked or cut-off reply is never parsed as an action; tell the model.
                message = (
                    f"Your previous reply could not be used ({exc}). Reply again, "
                    "shorter, with THOUGHT and one bash block."
                )
                done, _ = env.note(message, message="", model_name=self.model.name)
                if done:
                    return
                turns += [("assistant", "(no usable reply)"), ("user", message)]
                continue
            except ModelUnavailable:
                raise
            command, error = parse_command(reply.text)
            if error:
                done, _ = env.note(
                    error, message=reply.text, model_name=reply.model, metrics=reply.metrics()
                )
                if done:
                    return
                turns += [("assistant", reply.text), ("user", error)]
                continue
            tool, args = ("submit", {}) if command == SENTINEL else ("bash", {"command": command})
            observation, done, _ = env.step(
                Action(
                    tool=tool,
                    arguments=args,
                    message=reply.text,
                    model_name=reply.model,
                    metrics=reply.metrics(),
                    llm_calls=1,
                )
            )
            if done:
                return
            turns += [("assistant", reply.text), ("user", render(observation))]


def make_baseline(provider: str = "gemini", interval: float = 6.0):
    model = from_config(provider, interval)

    def factory(task):
        return BaselineAgent(model)

    return factory
