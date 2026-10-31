# T34: the worker reads DeepSeek's native tool-call markup as a proposal

- **Assignee:** Claude (GLM-5.3 GO required)
- **Branch:** `task/T34-dsml-tool-calls`
- **Line budget:** ≤ 30 added lines in `m2k/`
- **Files you may change:** `m2k/worker.py`, `tests/test_dsml.py`, `tasks/README.md`
- **Status:** DONE

## Goal
In the T28 runs on GitHub, DeepSeek sometimes answered in its own tool-call markup (DSML) instead of the JSON object
the system prompt asks for. One example: `<｜DSML｜invoke name="read_file">` with `<｜DSML｜parameter name="path" …>`.
Each such reply was `proposal.invalid`. Three in a row stopped run p1-1 as STALLED after four rounds.

The markup is still just a proposal (data): the same gates check it, and it has the same effect. So the worker can read
it, but nothing else changes.

## Required behaviour
1. `parse_proposal` tries JSON first, exactly as before. Only when the reply has no JSON object with a `tool` key does
   it look for DSML:
   - Exactly one `invoke name="<tool>"` block; its `parameter name="<key>"` children become `args`.
   - A parameter marked `string="false"` is parsed as JSON. If that fails, the reply is invalid. Any other parameter
     value is kept as a raw string, byte for byte (it can hold code with `<`, `{` and newlines).
   - The thought is the text before the markup.
   - The marker spellings seen so far are all accepted: `｜DSML｜` and `｜｜DSML｜｜`, with or without a space before
     the tag name.
2. Two or more `invoke` blocks make the reply invalid ("one tool call per reply"). A reply with neither JSON nor DSML
   is invalid, with the same error as before.
3. A JSON reply whose content contains DSML text (for example, `write_file` of a file that mentions the markup) is
   read as JSON, and the content is unchanged.
4. Only the worker changes. A reviewer reply in DSML stays unparseable, so the verdict is UNKNOWN, never PASS.

## Acceptance
`python -m unittest tests.test_dsml -v`, and the full suite.

## Review notes (written by reviewer)

**Round 1 (PR #32): PASS (Claude, 2026-10-14, after GLM GO), merged as #32.** Noted, not blocking:
- A DSML reply whose parameter value holds a JSON object with a `tool` key is read as that JSON. This follows the card
  (JSON first), and either way it is still a model proposal that the gates check.
- The spelling `｜DSML｜` with a space is not asserted in a test.

## Handoff note (written by implementer, ≤ 10 lines)
- `m2k/worker.py` (+28): `_dsml` reads one `invoke` block into `{tool, args, thought}`. `parse_proposal` tries JSON
  first and falls back to DSML only when there is no JSON proposal with a `tool` key.
- `tests/test_dsml.py`: 7 tests, covering the spellings, raw values, `string="false"`, two calls, JSON first, the
  gate denial through `Kernel`, and a DSML review giving UNKNOWN. Full suite green locally and on CI.
- Not done: DeepSeek's native API tool calling (tools passed in the request). The markup is accepted as text only.
