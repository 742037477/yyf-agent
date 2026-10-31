# T36: a busy model provider is asked again; a failed planner call is a model error

- **Assignee:** Claude (GLM-5.3 GO required)
- **Branch:** `task/T36-model-busy-retry`
- **Line budget:** ≤ 40 added lines in `m2k/`
- **Files you may change:** `m2k/models.py`, `m2k/kernel.py`, `tests/test_busy_retry.py`, `.github/workflows/m2k-task.yml`, `tasks/README.md`
- **Status:** DONE

## Goal
T29 runs 1 and 2 never reached the real work.
- Run 1: DeepSeek did not start the planner request within 900 s.
- Run 2: DeepSeek returned `HTTP 503: Service is too busy`.

Both are moments when the provider did no work and used no tokens. A wait and one more try would have gone through.
The ledger also reported both as `PLAN_REJECTED` ("no proposed plan passed the plan rules"), which is wrong: no plan
was ever proposed.

## Required behaviour
1. `OpenAICompatModel(retry_delays=...)` takes seconds to wait before each resend; the default is `()`, which means
   one attempt, exactly as before.
2. Only a busy provider is asked again:
   - HTTP 429, 500, 502, 503 or 504;
   - or a response with `error` and no `choices`.

   These raise `ProviderBusy` (a `ModelError`). After the last delay the error is raised with
   `(gave up after N attempts)`.
3. Nothing else is sent again: other HTTP codes, network errors, and an empty answer cut off by `finish_reason=length`
   (that one used tokens, which the ledger must count).
4. A resend happens inside one `complete` call. The ledger still records one `model.called` with the usage of the reply
   that answered, because a busy reply has no usage.
5. When the planner model fails, the parent stops with `MODEL_ERROR` and the reason `planner: <error>`. A plan that was
   proposed and broke the rules is still `PLAN_REJECTED`.
6. `m2k-task` gives both models `retry_delays = [60, 240]`. The worst case is two extra waits of 5 minutes in total,
   well inside the job's 60 minutes.

## Acceptance
`python -m unittest tests.test_busy_retry -v`, and the full suite.

## Review notes (written by reviewer)

**Round 1 (PR #35): PASS (Claude, 2026-10-15, after GLM GO), merged as #35.** `card-scope` failed at first because the file list was
wrapped onto two lines, and the scope check reads one line; the list is now on one line. Non-blocking:
- A client-side socket timeout is not resent. Only error bodies and 429/5xx are resent, as the card says.
- "Last `model.error`" equals "the planner failed" only while planning is the parent's first model call.

## Handoff note (written by implementer, ≤ 10 lines)
- `models.py`: `_post` raises `ProviderBusy` for 429/5xx and for error bodies; `complete` waits and resends while
  `retry_delays` lasts. With no delays, the old message and its `__cause__` are kept.
- `kernel.py`: when a plan run has no subtasks and the parent's last `model.error` exists, the stop is `MODEL_ERROR`.
- `tests/test_busy_retry.py`: 6 tests. Full suite green locally.
- Not done: resending after `finish_reason=length`. That needs the worker to record the wasted call first.
