# T11: a dropped model connection is UNKNOWN, never a crash

- **Assignee:** Codex (GPT-6)
- **Branch:** `task/T11-model-connection-errors`
- **Line budget:** ≤ 10 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/models.py`, `tools/pr_review.py`, `tests/test_models.py`
- **Status:** DONE

## Goal
Twice on PR #8 the GLM `review` job ran for about 4.5 minutes and then failed without posting a comment.
The cause: `OpenAICompatModel.complete` turns only `HTTPError`, `URLError`, `TimeoutError` and `JSONDecodeError`
into `ModelError`, so other network failures escape as raw exceptions. Examples:
- the server closes the connection while the response is pending (`http.client.RemoteDisconnected`,
  `ConnectionResetError`, both `OSError`);
- the body is cut off mid-read (`http.client.IncompleteRead`, an `HTTPException`);
- the body is not valid UTF-8 (`UnicodeDecodeError`).

Callers catch only `ModelError`, so in these cases:
- the PR review script crashes before it can post its UNKNOWN comment;
- inside the kernel, a dropped worker or reviewer connection crashes the whole run instead of being recorded.

Unknown must never be a crash.

## Required behaviour
1. `OpenAICompatModel.complete` turns every failure of the request and of reading or decoding the response into
   `ModelError("<ExceptionType>: <message>")`. That covers `OSError` (which includes `URLError` and `TimeoutError`),
   `http.client.HTTPException`, `json.JSONDecodeError` and `UnicodeDecodeError`. `HTTPError` keeps its current
   `HTTP <code>: <detail>` message. Do not catch bare `Exception`.
2. `tools/pr_review.py` always posts a comment:
   - When the model call fails, it posts the existing UNKNOWN comment.
   - Any other unexpected exception (finding the card, running git diff, building the prompt) is also posted as
     `### GLM review: UNKNOWN` with the exception type and message. The script then exits 1.
   - If posting the comment itself fails, print the error and exit 1. Do not loop or retry.

## Out of scope
- Automatic retries, longer timeouts, or streaming.
- Any change to the kernel, worker or verify. They already turn `ModelError` into FAIL or UNKNOWN.

## Acceptance
```
python -m unittest tests.test_models -v
python -m unittest discover -s tests -t .
```
Tests patch `urllib.request.urlopen` and check that each of these makes `complete` raise `ModelError`:
- `http.client.RemoteDisconnected` raised by `urlopen`;
- `ConnectionResetError` raised by `resp.read()`;
- `http.client.IncompleteRead` raised by `resp.read()`;
- a body of invalid UTF-8 bytes.

They must also show that `HTTPError` still gives the `HTTP <code>:` message.

## Review notes (written by reviewer)

**Round 1 (PR #9): PASS (Claude, 2026-10-13, after GLM GO), merged as #9.** ci + card-scope green; restoring the old
narrow except makes 7 model tests error. GLM ran on the new review script and posted normally.

## Handoff note (written by implementer, ≤ 10 lines)

- Baseline: main `70c05c8`; 139 tests passed (1 platform skip); new mocked disconnect/read/decode cases escaped as raw exceptions.
- Wrapped request/response failures, including interrupted HTTP error bodies, in ModelError; preserved HTTP status/detail messages.
- PR review now posts UNKNOWN for preparation errors (including find_card SystemExit); failed posting prints the error and exits 1 without retries.
- Verified: `python -m unittest tests.test_models -v` (20 tests); full suite (151 tests, 1 platform skip); `git diff --check`.
- Only the three allowed implementation/test files plus this handoff changed; m2k added lines: 6/10.
- Local verification: Windows, Python 3.13.11; no real model calls or PR comments. CI and reviewer acceptance pending; reviewer opens the PR.
