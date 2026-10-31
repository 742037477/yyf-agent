# T31: CI spends fewer GitHub Actions minutes without losing coverage

- **Assignee:** Claude (workflow only; GLM-5.3 GO required)
- **Branch:** `task/T31-ci-minutes`
- **Line budget:** 0 added lines in `m2k/`
- **Files you may change:** `.github/workflows/ci.yml`, `.github/workflows/main-guard.yml`
- **Status:** DONE

## Goal
The free Actions minutes ran out on 2026-10-14. Each PR used about 40 billed minutes: the tests ran twice (once for
the push, once for the PR), and Windows runners count double. Owner decision (2026-10-14): PRs run Linux only, and
Windows runs after the merge to main.

## Required behaviour
1. **`ci.yml` triggers.** `pull_request`, plus `push` to `main` only, with `paths-ignore: ["tasks/**", "docs/**"]`.
   Pushes to task branches no longer run CI on their own: their PR does.
2. **Matrix.** On a `pull_request`: `ubuntu-latest` with Python 3.11 and 3.13. On a push to `main`: `ubuntu-latest`
   and `windows-latest`, each with 3.11 and 3.13.
3. **`main-guard`** moves unchanged to its own workflow `main-guard.yml`, triggered on every push to `main` (no path
   filter), so a direct push is still flagged whatever it touches.
4. `card-scope` and the GLM review are unchanged.

## Acceptance
- This PR shows exactly two `test (ubuntu-latest, …)` jobs, plus `card-scope` and `review`, and no Windows job.
- After the merge, the push to `main` runs the four test jobs and `main-guard`.
- A later commit touching only `tasks/` runs `main-guard` but no tests.

## Review notes (written by reviewer)

**Round 1 (PR #29): PASS (Claude, 2026-10-14, after GLM GO), merged as #29.** The PR ran exactly two Linux test jobs
plus card-scope and review. Later: mirror `paths-ignore` under `pull_request` if card-only PRs should skip tests.

## Handoff note (written by implementer, ≤ 10 lines)
- `ci.yml`: PR → Linux 3.11/3.13; push to main (not tasks/docs only) → Linux + Windows; `main-guard.yml` on every main push.
- Verified on PR #29 itself: two Linux jobs, card-scope, review; no Windows job.
