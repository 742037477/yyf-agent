# T33: the GLM review runs once, when the reviewer asks for it

- **Assignee:** Claude (workflow only; GLM-5.3 GO required)
- **Branch:** `task/T33-review-on-label`
- **Line budget:** 0 added lines in `m2k/`
- **Files you may change:** `.github/workflows/glm-review.yml`
- **Status:** DONE

## Goal
The GLM first-pass review is the longest job, about 5 minutes, most of it spent waiting for GLM to think. It ran again
on every push to a PR, so many PRs were reviewed two or three times. Owner decision (2026-10-14): review once, when
the PR is ready, and keep the reasoning at maximum.

## Required behaviour
1. `glm-review.yml` runs on `pull_request` `labeled` only, and only when the label is `glm-review`. Pushes, opening and
   reopening no longer start a review.
2. To review again after a fix, the reviewer removes the label and adds it again.
3. The job uses the runner's own `python3` instead of `actions/setup-python` (the script is standard library only).
   This saves the setup step.
4. The review itself is unchanged: same script, same model and reasoning, same card-based prompt.

## Acceptance
- Opening this PR runs CI but no review.
- Adding the label `glm-review` runs exactly one review, and it posts its comment.

## Review notes (written by reviewer)

**Round 1 (PR #30): PASS (Claude, 2026-10-14, after GLM GO), merged as #30.** Verified on this PR: opening ran CI
only; adding the label ran one review (43 s). The reviewer adds the label only when a PR is ready.

## Handoff note (written by implementer, ≤ 10 lines)
- `glm-review.yml`: `labeled` + `glm-review` only; `setup-python` dropped (the script and `m2k.models` are stdlib only,
  checked by importing it with `cryptography` blocked). Label `glm-review` created in the repository.
