# T35: Windows CI only for code changes, with one Python version

- **Assignee:** Claude (workflow only; GLM-5.3 GO required)
- **Branch:** `task/T35-windows-ci-minutes`
- **Line budget:** 0 added lines in `m2k/`
- **Files you may change:** `.github/workflows/ci.yml`, `tasks/README.md`
- **Status:** DONE

## Goal
Actions minutes this month are about 2740 Linux-equivalent minutes. 65% of them are the Windows jobs that run after
each merge to main, because Windows minutes count double. The owner decided on 2026-10-15 to cut most of that.
Windows support is still checked every time code changes.

## Required behaviour
1. A push to `main` runs `ci` only when it changes `m2k/**`, `tests/**`, `tools/**` or `pyproject.toml`. Changes to
   workflows, task cards or docs alone no longer start it.
2. On such a push the matrix runs Linux with Python 3.11 and 3.13, and Windows with 3.13 only (no Windows 3.11).
3. PRs are unchanged: Linux 3.11 and 3.13, plus `card-scope`.
4. Job names keep the form `test (<os>, <python>)`.

## Acceptance
- This PR runs Linux 3.11 and 3.13 and `card-scope`, with no Windows job.
- The first later merge that changes code runs exactly three test jobs: Linux 3.11, Linux 3.13 and Windows 3.13.
  Claude checks this and notes it here.

## Review notes (written by reviewer)

**Round 1 (PR #34): PASS (Claude, 2026-10-15, after GLM GO), merged as #34.**
- PR side: Linux 3.11 and 3.13 plus `card-scope`, with no Windows job.
- Push side: verified on the merge of T36 (#35, run 38668744918). It ran exactly Linux 3.11, Linux 3.13 and
  Windows 3.13; all green, and `card-scope` was skipped.

## Handoff note (written by implementer, ≤ 10 lines)
- `ci.yml`: `paths` replaces `paths-ignore` on push. The matrix is os × python with two excludes: Windows 3.11 always,
  and Windows entirely on `pull_request` (an `exclude` whose `os` comes from an expression, `none` on push).
- Verified: the YAML parses; this PR's checks show the PR side. The push side is checked on the next code merge.
