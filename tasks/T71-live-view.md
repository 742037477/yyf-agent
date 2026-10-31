# T71: `m2k live` — the approved animation, driven by the real ledger (follow live + replay); record the startup refusals that are missing from the ledger

- **Assignee:** Codex (backend, refusal events, tests) + Claude (the page `m2k/live.html`, on the same branch) — GLM-5.3 GO required; the owner accepts it in a browser
- **Branch:** `task/T71-live-view` (start from the latest main, after T72 is merged)
- **Line budget:** ≤ 1500 added lines in `m2k/` in total (the CI check counts every file): about 1100 of them are Claude's page
  `m2k/live.html`; everything else in `m2k/` stays ≤ 480; tests not counted
- **Files you may change:** `m2k/live.py` (new), `m2k/live.html` (new), `m2k/cli.py`, `pyproject.toml`, `README.md`,
  `tests/test_live.py` (new), `tests/test_startup_refusals.py` (new), `tasks/README.md`, `docs/live/m2k-flow-demo.html`,
  and, only as section A allows: `tests/test_trust_required.py`, `tests/test_author_reviewer.py`,
  `tests/test_external_agent.py`, `tests/test_obs.py`, `tests/test_small_fixes.py`, `tests/test_anchor.py`, `tests/test_release.py`,
  `tests/test_sandbox.py`
- **Status:** DONE (merged #54, bb15ccd)

## Goal
The owner approved an animation of a run (`docs/live/m2k-flow-demo.html`, artifact "M2 治理流水线", version 5): a
startup strip with 11 self-check lamps, an 11-station pipeline, pixel animals for agents and models, an OBS notary that
stamps key events and reconciles at the read-only adjudicator, and an append-only ledger tape. Today it plays a
script. After this card, `m2k live` serves the same picture **driven by the real ledger**: follow a run while
`m2k run` works, or replay a finished one. It is an **observer only**: it reads the ledger (and, for one endpoint, a
private copy of it) and never takes part in a decision.

Division of work (owner, 2026-10-19): **Codex builds the backend** — the local server, the read-only ledger API, and
the mapping from ledger events to animation steps (`m2k/live.py`), plus the missing refusal events in `cli.py`.
**Claude builds the page** (`m2k/live.html`) from the prototype. Codex creates `m2k/live.html` only as a placeholder
(≤ 40 lines: a title and a plain list of `/api/steps` results) so the server and tests work; Claude replaces it on the
same branch before the PR is opened. Codex must not try to draw the animation.

## Required behaviour

### A. Record the startup refusals that leave no trace today (`cli.py`)
After the task contract is loaded, every refusal of `m2k run` must append
`m2k.startup / startup.refused {"task": <task id>, "code": <CODE>, "reason": <text>}` exactly like the existing ones,
keep its current stderr text and exit code 2, and never turn into a traceback. Today these refuse without a ledger
event:

| Situation (current code) | CODE |
| --- | --- |
| no `[trust]` and no `--untrusted` | `NO_TRUST` |
| `[trust]` together with `--untrusted` | `CONFIG` |
| `[obs]` table missing a key / wrong type (`配置错误：[obs] …`) | `OBS_CONFIG` |
| OBS key or log inside the workspace; OBS key does not match `public_key_b64` (today a `KernelError` → `SystemExit`) | `OBS_CONFIG` |
| anchor file or `repo_dir` inside the workspace (today `SystemExit`) | `ANCHOR_CONFIG` |
| no usable command sandbox | `SANDBOX_UNAVAILABLE` |
| worker and reviewer are the same model without `allow_same_model` (T72) | `SAME_MODEL` |
| external worker probe fails (`Worker 命令试启动失败`) | `WORKER_PROBE` |

If the ledger itself cannot be opened, print the refusal as today, add one line `（账本打不开，本次拒绝未记账：<error>）`,
and still exit 2. Errors **before** the contract is loaded (config file missing, `[models.*]` missing or of the wrong
type, a non-boolean `allow_same_model`, `[sandbox]` not a table, an invalid contract) have no task id: leave them stderr-only. Do not change any check,
order, wording or exit code otherwise.

**Existing tests (owner decision, 2026-10-19).** Several existing tests assert that one of these refusals leaves no
ledger (or no event). Change exactly those assertions, and only them, to: the ledger holds exactly one new
`m2k.startup / startup.refused` with the expected code naming the task, and nothing else was appended. Keep every
other assertion in those tests unchanged (stderr text, exit code, models / sandbox / OBS not called, no task
submitted). Refusals that happen before the contract is loaded keep their "no ledger" assertions. List each changed
test in the handoff note.

### B. The server (`m2k/live.py`, wired in `cli.py`)
1. `m2k [--config C] [--ledger L] live [--task ID] [--port 8765] [--no-browser]`. The ledger path is resolved like
   the other read-only commands. Serve on **127.0.0.1 only**, print the URL, open the browser unless `--no-browser`.
   Standard library only (`http.server.ThreadingHTTPServer`, `sqlite3`, `json`, `webbrowser`, `tempfile`). Ctrl+C
   stops cleanly; a port in use → one clear line, exit 1.
2. **Read-only.** Read the ledger with `sqlite3.connect("file:<path>?mode=ro", uri=True)`. Never construct
   `Ledger(...)` on the real file (its constructor writes the schema), never write next to it, never start OBS, never
   call a model, never touch trust or anchor files. A missing ledger file → the API answers with an empty result and
   `"ledger": "missing"`, and the page keeps polling.
3. **API** (JSON, GET only; other methods → 405; bad parameters → 400 with a JSON error, never a traceback):
   - `/` → `m2k/live.html` (package data in `pyproject.toml` if needed).
   - `/api/tasks` → newest first: each top-level task id with first/last seq and time, last kind, final verdict if
     any, and `"refused": <CODE>` for task ids that appear only in `m2k.startup` refusals (a refused run still shows
     up, with its lamps). Subtasks are not listed separately.
   - `/api/steps?task=<id>&after=<seq>&limit=<n≤500>` → `{"ledger": "ok"|"missing", "steps": [...], "last_seq": n,
     "done": bool}`: animation steps (section C) for the task, its subtasks (all descendants via
     `task.submitted.parent`), and the `m2k.startup` events whose `payload.task` names it, with `seq > after`, by
     seq. `done` is true after `delivery.result`, `delivery.refused`, a top-level `verdict` with no delivery
     configured, or `startup.refused`. Steps are recomputed from all of the task's events on each request and then
     filtered by `after` (stateless; tasks have at most a few thousand events).
   - `/api/events?task=<id>&after=<seq>&limit=<n≤500>` → the raw events behind those steps with a **trimmed
     payload** (strings over 300 characters cut with `…`; nesting deeper than 3 replaced by `"…"`; the fields
     `output`, `prompt`, `envelope`, `receipt`, `signature_b64` replaced by `"<n chars>"` or `"<signed>"`). The page
     shows it when a step is clicked.
   - `/api/adjudicate?task=<id>` → re-run the read-only adjudicator **now**, on a private copy: copy the ledger with
     `sqlite3` `backup()` from the read-only connection into a temporary file, open `Ledger` on the copy, call
     `adjudicate(ledger, task, **adjudication_inputs(cfg, base))` (with `--config`; without it, no OBS inputs), return
     `{"verdict", "reasons", "flagged_seqs"}` where `flagged_seqs` are the ledger seqs named in OBS-related reasons
     (regex `seq (\d+)` on reasons that mention OBS or a receipt), then delete the copy. This is how the page shows
     a ledger tail that was forged **after** the run (owner's "伪造账本尾巴" scene): the kernel's own
     `gate.adjudication` was written before the forgery, so only a fresh read-only adjudication can show it. Display
     only: the result is never written anywhere.

### C. Event → animation steps (the mapping lives in `m2k/live.py`, pure functions, unit-tested)
One step per ledger event, except `obs.receipt`, which is folded into the event it witnesses
(`receipt["body"]["ledger_seq"]` → that step gets `"obs": true`; display only, no signature check — the adjudicator
is the authority). A step:

```json
{"seq": 4031, "ts": "...", "task": "wordstats-1", "kind": "effect.intent",
 "station": "worker", "state": "active|ok|bad|warn|unknown", "obs": false,
 "lamps": {"8": "ok"},
 "actor": {"role": "worker|reviewer|planner", "animal": "whale|crab|dragon|fox|robot",
           "do": "enter|files|cmd|edit|home|think|busy|done|denied|verdict"},
 "caption": "提议 write_file wordstats.py → Gate 放行",
 "detail": {"model": "...", "round": 2, "tokens": 4870, "call": "write_file wordstats.py",
            "tree": [["expenses-summary", "run"]], "review": "same-model|cross-model"}}
```
Only the keys that apply are present.
- **Stations** (the prototype's ids): `contract` task.submitted · `cred` credential.issued · `worker` model.called
  (role worker/planner), effect.*, gate.denied, plan.*, budget.*, subtask.*, repair.*, worker.stopped, model.error,
  and a subtask's own test/review/verdict · `frozen` candidate.frozen · `test` test.result · `review` model.called
  (role reviewer), audit.result · `verdict` verdict · `adjud` gate.adjudication · `anchor` anchor.checkpoint,
  anchor.published, anchor.failed · `gate` delivery.result, delivery.refused · `verify` is never lit from the ledger
  (`check-delivery` does not write it). `obs.failed` → the station of `payload.kind`, state `bad`. Unknown kinds →
  `station: null`, caption = the kind; they must never raise.
- **Lamps** (1 配置与契约, 2 信任链, 3 发行清单, 4 OBS 启动, 5 锚点核对, 6 框架指纹, 7 授权书, 8 Root 记忆, 9 命令隔离,
  10 审查独立, 11 Worker 试启动). `startup.refused` code → lamp: `CONFIG` 1; `RELEASE*` except `RELEASE_ROLLBACK` 3;
  `OBS_REQUIRED`, `OBS_CONFIG` 4; `OBS_UNAVAILABLE` 8 if this task already has `release.checked`, else 4; `ANCHOR_*` 5;
  `FRAMEWORK*` 6; `CHARTER*`, `EXCEEDS_CHARTER` 7; `ROOT_CHANGED`, `RELEASE_ROLLBACK`, `OBS_REFUSED` 8;
  `SANDBOX_UNAVAILABLE` 9; `SAME_MODEL` 10; `WORKER_PROBE` 11; any other code 2. That lamp is `bad`,
  the lamps before it `ok`, the rest untouched. `anchor.warning` → lamp 5 `warn`. `release.checked` → lamp 8 (`ok`
  once its receipt is folded in). On `task.submitted` all lamps not yet set become `ok`, except: `trust` null → 2, 3,
  7, 8 `skip`; `obs` null → 4, 8 `skip`; `sandbox` `"none"` → 9 `warn`; `review` `same-model` (T72) → 10 `warn`.
- **Animals** from `task.submitted.worker_model` / `reviewer_model` (case-insensitive substring): `deepseek` or
  `dsh` → whale, `claude` → crab, `codex`, `gpt` or `openai` → dragon, `glm` → fox, else robot. Actor `do`:
  credential.issued → worker `enter`; effect.intent by tool: `list_files`/`read_file` → `files`, `write_file`/
  `edit_file` → `edit`, `run_command` → `cmd`, `external_agent` → `busy` (caption says the framework cannot see inside
  an external agent); effect.result → `home`; gate.denied → `denied`; model.called role worker/planner → `think`;
  model.called role reviewer → reviewer `enter`; audit.result → reviewer `verdict` (GO/NO_GO in the caption).
- **Captions** are short Chinese lines built from the payload (the prototype's wording is the guide: "被拦下：<reason>",
  "任务方的验收：<command> → 退出码 0", "内核判定 PASS"). Never put prompts, outputs or secrets in a caption.
- **Detail:** worker model and round from `model.called`; `tokens` = running sum of the worker's reported usage, or
  `null` when not reported (unknown is not zero); the subtask tree from `task.submitted.subtasks` / `plan.accepted`,
  `subtask.started`, child `test.result`, `subtask.finished`.

### D. The page (Claude, not Codex)
`m2k/live.html`: the prototype made event-driven — polls `/api/steps` about twice a second in live mode, plays the
steps with the prototype's motions (catching up faster when behind), replays a finished task at 0.5×–8×, task picker
from `/api/tasks`, click a step for `/api/events` detail, and at the end of a task (or on click of 只读裁决) calls
`/api/adjudicate` and plays the OBS reconciliation (green ticks; red on `flagged_seqs`). One self-contained file, no
external scripts, fonts or images (system fonts only), works offline. The script scenes stay available as "演示剧本".

### E. Docs
`README.md`: a short section "实时观看运行（m2k live）" — run it in a second terminal during `m2k run`, or later to
replay; it only reads the ledger; what the animals and lamps mean; an external agent's inside is not visible; the
"重新裁决" button re-runs the read-only adjudicator on a copy.

## Out of scope
- Any change to the kernel, gates, trust, OBS, anchor or ledger code, verdict or adjudication rules. The only new
  ledger writes are the `startup.refused` events of section A.
- Observing an external agent's workspace (T73); agent self-reports; authentication or remote access; sound.
- Local separate-user isolation (T66/T68): later the owner runs `m2k live` as himself against a ledger he can read.

## Acceptance
```
python -m unittest tests.test_live tests.test_startup_refusals -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
- `tests/test_startup_refusals.py`: each row of the table in A → exit 2, unchanged stderr text, one
  `startup.refused` with that code naming the task; a ledger that cannot be opened → exit 2, no traceback.
- `tests/test_live.py` (server on an ephemeral port in a thread; ledgers built in setup with `Ledger` and the
  existing scripted-model helpers): binds only 127.0.0.1; `/api/tasks` lists a refused-only task with its code;
  `/api/steps` for a full scripted PASS run yields the stations in order `contract, cred, worker…, frozen, test,
  review, verdict, adjud, …, gate`, `after`/`limit` work, OBS-witnessed events carry `"obs": true` when receipts are
  present (build them with the existing OBS test helpers, or by hand); every refusal code maps to the lamp in C;
  `trust` null → lamps 2, 3, 7, 8 `skip`; an unknown kind yields a step with `station: null`; trimming in
  `/api/events`; bad parameters → 400 JSON; POST → 405; the ledger file's bytes and mtime are unchanged after many
  requests **including `/api/adjudicate`**, and no temporary copy is left behind; a missing ledger → `"missing"`;
  **forged tail**: after a scripted run with OBS, append a `verdict` PASS by hand with `Ledger.append` (valid hash
  chain, no receipt) → `/api/adjudicate` is not PASS and `flagged_seqs` contains that seq; `/` returns the page and
  it has no `http://`/`https://` references in `src=`, `href=`, `@import`, `url(`.
- By hand on Windows (after Claude's page is in): replay one finished demo task from the owner's ledger
  (e.g. `dsh-root-20261018-174415-878284` or a `t69-codex-…` task) and follow one live demo run; two or three
  screenshots listed in the handoff note.

## After the merge (reviewer, not this card)
- No config or manifest changes. The owner gives feedback on the live page; art tweaks go into a follow-up card.
- T73 (framework observes the external agent's workspace) adds `workspace.changed`; this mapping then sends the
  worker animal to the 文件/编辑 stations.

## Review notes (written by reviewer)

1. `done` 不改内核、不加字段。规则改为：`done` 为 true 当且仅当出现 `startup.refused`、`delivery.result`、`delivery.refused`，或顶层任务（无 parent）的 `gate.adjudication`。它的含义是“运行已到最后阶段”，之后仍可能追加 `anchor.*` / `delivery.*` 事件；页面在 `done` 后继续轮询一小段时间再收尾（页面是 Claude 的事）。测试按这条规则写。

2. 原来以 `SystemExit(原文)` 退出的那几种拒绝（OBS 密钥/日志在工作区里或密钥不匹配 → `OBS_CONFIG`；锚点文件或 repo_dir 在工作区里 → `ANCHOR_CONFIG`）：退出方式保持现状——仍然 `raise SystemExit(同样的原文)`（退出码照旧），只是在 raise 之前追加那条 `startup.refused`。所以 `test_obs.py:681–682`、`test_anchor.py:289`、`test_release.py:741` 里 “抛出含原文的 SystemExit” 的断言不改；这些测试里若有“不留账本/不新增事件”的断言，按 A 节允许的方式改成“恰好一条带对应 code 的 startup.refused”。卡里 A 节 “keep … exit code 2” 对这两种情况理解为“保持现有退出方式”。

2026-10-19 Claude: tests/test_sandbox.py added under the same rule (RefusalAndRecordTest.test_auto_without_backend_refuses_before_submitting, the no-new-event assertion only).

- 2026-10-19 Claude: PASS on 9758669. Codex review: round 1 sent back one point (`openai_compat:glm-5.3` drawn as a dragon), fixed; round 2 PASS. GLM-5.3: round 1 NO_GO (by-hand Windows acceptance missing, handoff out of date), both done; one timeout; round 3 GO. CI and card-scope green. The card budget counts all of `m2k/` (1453 ≤ 1500; 353 outside the page).
- GLM non-blocking notes for later: the page polls `/api/tasks` every 500 ms after a run ends (back off later); D says 只读裁决 where the page button says 重新裁决.
- Ledger order seen in real runs: `delivery.result` (staged + receipt) is written before `anchor.checkpoint`; the release into `deliver_to` happens after the checkpoint.

## Handoff note (written by implementer, ≤ 10 lines)

- Codex: A/B/C/E per the three Review-note clarifications (`live.py` + `cli.py`). Claude: `m2k/live.html` = the approved v5 animation driven by `/api/steps` (live follow, replay 0.5×–8×, event detail, re-adjudication on a copy with OBS reconciliation; system fonts, offline; scripted demo when no server); after review: animal from the model name after the adapter prefix (`openai_compat:glm-5.3` → fox), captions for task.submitted / credential.issued / test.result.
- Tests: `test_live` 22 tests (incl. 3 new animal sub-cases) and `test_startup_refusals`, 33 new tests in total; full suite 741 OK (Windows: 730 + 11 skipped); `accept --offline` 28/28; T46 CHECK OK. `test_live` placeholder ≤ 40-line check replaced by a check that the page uses all four endpoints.
- Existing tests changed only from "no ledger / no new event" to "exactly one startup.refused with the code": test_trust_required (NO_TRUST, CONFIG), test_author_reviewer (SAME_MODEL ×5), test_external_agent (WORKER_PROBE), test_obs (OBS_CONFIG; SystemExit kept), test_small_fixes (SANDBOX_UNAVAILABLE ×2), test_anchor (ANCHOR_CONFIG), test_sandbox (SANDBOX_UNAVAILABLE). Pre-contract no-ledger assertions kept.
- By hand on Windows (owner's ledger, 2026-10-19): `claude-root-demo-20261019-091821-b8252e7d`, a Codex and a dsh task replayed through the API: 17 steps each, 9 OBS seals, adjudicate PASS, `flagged_seqs` empty; ledger 118,784 bytes and mtime unchanged; no copy left.
- Live follow: `dsh-live-demo-20261019-135525-ddc39505` (mid-run API: 4 steps, done=false; end: 17 steps, PASS, delivered) and `dsh-live-demo-20261019-141133-9b45c9f2` followed in Edge in real time from start to "运行结束 · 重新裁决 PASS", no page or console errors.
- Screenshots in `<demo-dir>\t71-shots\`: `replay-dsh.png`, `live-mid2.png`, `live-done2.png` (real-time Edge via Playwright). The earlier `--virtual-time-budget` captures froze at step 1 (reproduced on Linux with the same ledger; the page plays fully in real time), so they are not used.
- Not done: nothing in the card's scope. Status stays OPEN for CI and review.
