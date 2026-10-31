# T13: the GLM PR review streams its reply, so long reviews are not cut off

- **Assignee:** Codex (GPT-6)
- **Branch:** `task/T13-review-streaming`
- **Line budget:** 0 added lines in `m2k/` (tooling only)
- **Files you may change:** `tools/pr_review.py`, `tests/test_pr_review_stream.py`
- **Status:** DONE

## Goal
On PR #12 (T08, a 42 KB diff) the GLM `review` job failed three times with
`ConnectionResetError: [Errno 104] Connection reset by peer`, each time after about 4.5 minutes.
GLM reasons for a long time before it answers. During that time a non-streaming request sends nothing,
and the provider's gateway drops the idle connection. With `"stream": true` the server sends chunks
continuously, so the connection stays alive. T08 needs a GLM GO before it can merge, so it waits for this card.

## Required behaviour
1. `tools/pr_review.py` sends the review request itself, with `"stream": true` and
   `"stream_options": {"include_usage": true}`. It uses the same URL, model, key (`GLM_API_KEY`) and
   `max_tokens` as today.
   - Read the response line by line as server-sent events: `data: {json}` lines, ending with `data: [DONE]`.
   - Join every `choices[0].delta.content` into the reply text and ignore `reasoning_content`.
   - Take the token counts from the chunk that carries `usage`; if no chunk does, the counts are `None`.
   - Use a per-read socket timeout of 120 s (so a stalled stream fails) instead of one 600 s timeout for the whole call.
2. Everything after that is unchanged: the text goes through `extract_json`, and the comment format and exit codes stay
   as they are. Failure handling is the same as in T11: any HTTP, connection, timeout or decoding error, an empty
   reply, or a stream with no `[DONE]` posts the UNKNOWN comment and exits 1.
3. `m2k/` is not touched. The runtime reviewer inside m2k keeps the non-streaming adapter.

## Out of scope
- Streaming in `m2k/models.py`, retries, splitting large diffs.

## Acceptance
```
python -m unittest tests.test_pr_review_stream -v
python -m unittest discover -s tests -t .
```
Tests patch `urllib.request.urlopen` with a fake response that yields SSE lines (no real API).
- Chunks with content and reasoning give the joined content only, and the usage is read from the last chunk.
- A stream without a usage chunk gives `None` counts.
- A stream that ends without `[DONE]` gives UNKNOWN.
- `ConnectionResetError` raised mid-stream gives the UNKNOWN comment and exit 1.
- The request body contains `"stream": true`.

## Review notes (written by reviewer)

**Round 1 (PR #13): PASS (Claude, 2026-10-13, after GLM GO), merged as #13.** The PR was reviewed by its own streaming
script (live test OK); removing the [DONE] requirement fails a test.

## Handoff note (written by implementer, ≤ 10 lines)

- PR-only streaming transport sends stream/include_usage, joins content without reasoning, and requires [DONE].
- Keeps the configured URL/model/key/max_tokens, existing comments/exit codes, and T11 UNKNOWN handling; socket timeout is 120 s.
- Usage comes from the reported chunk; absent counts stay None. The m2k runtime adapter is unchanged.
- Baseline: origin/main bb558c1, 179 tests OK (1 skipped); new SSE tests failed before the implementation.
- Verified: tests.test_pr_review_stream (12 tests OK); full unittest suite (191 tests OK, 1 skipped); git diff --check clean.
- Only the two allowed implementation/test files plus this handoff changed; m2k added lines: 0/0.
- No real API called. Windows local checks passed; Linux CI and reviewer acceptance remain pending.
