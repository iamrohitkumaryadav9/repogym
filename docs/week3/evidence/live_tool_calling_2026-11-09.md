# Live integration tests: tool calling on the Gemini API free tier (2026-11-09)

`pytest -m live tests/integration/test_providers_live.py -s`, google-genai 2.33.0,
`generateContent` (D8), with the settings in `configs/models.yaml`. 9 of 9 passed.

| Check | gemini-3.5-flash-lite | gemma-4-31b-it | gemma-4-26b-a4b-it |
|---|---|---|---|
| First function call carries a thought signature | yes | yes | yes |
| 3-step multi-turn tool calling, with signatures replayed verbatim | bash, bash, submit | bash, bash, submit | bash, bash, submit |
| Middle step dropped and a summary part added to the task message: accepted (HTTP 200) | yes, then submit | yes, then submit | yes, then submit |
| `countTokens` (full `generateContentRequest`) against reported `prompt_tokens` | 519 / 520 | 519 / 520 | 519 / 520 |

Notes:
* gemma-4-31b-it returned 500 INTERNAL on every thinking probe earlier the same day
  (`api_probes_gemma-4-31b-it.json`). With the default settings (no `thinking_level`), all of
  its calls here succeeded.
* The first run of these tests failed: the unit-test network guard (`tests/conftest.py`)
  blocked the sockets, and the provider retried them as transport errors. Tests marked `live`
  are now exempt. Those 6 local attempts per test were counted in the quota ledger even
  though they never reached the API, which over-counts in the safe direction.
