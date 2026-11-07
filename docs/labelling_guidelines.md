# Labelling guidelines

These definitions are used for the human labels in `data/labelling/`. Those labels calibrate the
LLM task-type classifier and the automatic spec-clarity check. Follow them exactly, so that
someone else labelling the same tasks would get the same answers.

## Before you start

- Label blind. Don't open the `*.key.json` files next to the sheets until you've finished. They
  hold the GitHub-label types and spec-check flags you're being compared against.
- Write exactly one of the allowed values in the label column: lower case, no extra words. Use
  the `notes` column for anything else, such as uncertainty or why a task was hard to call.
- Use the issue and PR links when the excerpt is cut off. The excerpts are shortened, and the
  whole issue is what counts.

## Task type: `task_type_sheet.csv`

Column: `my_label (bug / feature / other)`

| Label | Use it when |
|---|---|
| `bug` | Behaviour is wrong compared with what the docs, the API or common sense promise: crashes, wrong output, regressions, bad error handling. |
| `feature` | The issue asks for new behaviour, or for an option, parameter or API that didn't exist before, even if it's phrased as a complaint. |
| `other` | Refactors, performance work where the output wasn't wrong, deprecation changes, typing-only changes and docs changes. |

**Tie-breaker:** ask "did the old code break a promise?"

- Yes: it's a `bug`.
- No, it just lacked something: it's a `feature`.

Examples of how to apply this:

- "Option X is ignored when Y is set": `bug`. The option promised an effect it didn't have.
- "It would be nice if X accepted a callable": `feature`, even if the title says "X doesn't work
  with callables".
- "Unhelpful error message": `bug` if the old error was misleading or wrong (bad error
  handling). `feature` if the old message was accurate and the request is for more detail.

## Spec clarity: `spec_sheet.csv`

Column: `my_spec_clear (yes / no)`

| Label | Use it when |
|---|---|
| `yes` | A skilled engineer who reads only the issue (and the codebase) could write a fix that passes these tests. |
| `no` | The tests depend on something the issue doesn't reveal: a new function, constant or parameter name; an exact error message; or one specific choice among several reasonable behaviours. |

Judge from the issue alone. Don't count the PR description, the review discussion or the gold
patch. The test patch excerpt is there only so you can see what the tests require; it isn't a
hint the solver gets.

Things that don't make a task unclear:

- Names that already exist in the codebase at the base commit. The solver can read those.
- Test helper names that are defined inside the test patch itself.
- Exact output that follows directly from the issue, such as the correct value the issue says
  should be returned.
