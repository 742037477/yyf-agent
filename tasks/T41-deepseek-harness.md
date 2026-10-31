# T41: DeepSeek Harness as the third external agent; clearer probe failures

- **Assignee:** Codex (the real run is started by the Codex desktop app on the owner's Windows computer)
- **Branch:** `task/T41-deepseek-harness`
- **Line budget:** ≤ 40 added lines in `m2k/`
- **Files you may change:** `m2k/external.py`, `m2k/cli.py`, `tests/test_external_agent.py`, `tests/fixtures/fake_agent.py`, `examples/dsh_demo/*`, `tasks/README.md`
- **Status:** DONE (#40)

## Goal
The owner wants DeepSeek Harness (`dsh`, npm `@deepseek-ai/dsh`, pre-release) governed like Codex (T37) and Claude Code
(T38) before stage 5.

What is known about its one-shot mode:
- it runs as `dsh --profile headless "<job>"`;
- **the prompt is a positional argument, not stdin**;
- it prints only the final answer, with no token usage;
- it needs `DEEPSEEK_API_KEY` in its environment;
- its main tool is a persistent shell with no sandbox on Windows.

Today's real runs also showed two rough edges, fixed here as well:
- a probe failure printed a raw JSON tail where one line ("Not logged in · Please run /login") was the answer;
- `m2k run` printed "已记入账本" warnings before a probe refused, although nothing was recorded.

## Required behaviour
1. `CliAgent(prompt="stdin" | "arg")`, default `"stdin"` (unchanged). With `"arg"`:
   - the prompt is appended as the last argv element;
   - stdin is empty;
   - the same applies to `probe()`.
2. `probe()` failures are readable:
   - when `command[0]` ends in `.ps1`, `.cmd` or `.bat`, the reason says it is a launcher script and asks for the real
     program (`.exe`, or `node <entry>.js`);
   - when the output holds a JSON object with `"is_error": true` and a string `result`, that string comes first in the
     reason.
3. In `cmd_run`, the probe runs before the `--untrusted` and `backend = "none"` warnings are printed. A refused run
   prints only the refusal.
4. `examples/dsh_demo/`: the same task and owner check as the Codex demo (`seed = "../codex_demo/seed"`), id
   `dsh-demo-1`, and a config `m2k.dsh.toml`:
   - `command = ["node", "<path to the dsh entry .js>", "--profile", "headless"]`, with a comment telling the owner
     how to find the entry (`npm root -g`), because the npm `.cmd`/`.ps1` shims mangle multi-line arguments;
   - `prompt = "arg"`;
   - `env` = the Windows list from the Claude config, plus `DEEPSEEK_API_KEY` and `DSH_HOME`;
   - `usage = ""`, so tokens are unknown and never zero;
   - `isolation = "none: DeepSeek Harness runs its shell tool without a sandbox on Windows (declared, not verified by M2)"`;
   - GLM as the reviewer.

## Acceptance
- `python -m unittest tests.test_external_agent -v` and the full suite.
- New tests:
  - prompt as an argument (the fake agent records it and sees an empty stdin);
  - a launcher-script command gets the clear reason;
  - a JSON `is_error` result is shown first;
  - a refused run prints no "已记入账本" warning.
- Owner, after merge (through the Codex desktop app): run the dsh demo once. It must be PASS, or else stopped by the
  probe with a clear reason. Claude records the facts.

## Out of scope
- Restricting dsh's shell tool: not documented for headless. Recorded honestly in `isolation`.

## Review notes (written by reviewer)
- 2026-10-17 Claude: PASS → squash 合并 #40。GLM GO；ci / card-scope 绿；env 清单与 Claude demo 一致外加 DEEPSEEK_API_KEY、DSH_HOME，任务与验收同 codex_demo。
- 不阻断：prompt="arg" 在 Windows 上受命令行约 32K 字符限制，修复或改派的长提示会启动失败（记 FAILED，不会误判 PASS）。
- 2026-10-17 真跑（用户授权，经 Claude 派 Codex 执行，main f6ad59d）：dsh-demo-1 **PASS**。dsh 0.2.0-rc.2，`node lib\bin.js --profile headless`，prompt 走参数；试启动通过；worker FINISHED（exit 0），验收 PASS，GLM GO，交付且 post_verified。用量 tokens 2822 只含审查一次，dsh 一次记为未知（tokens_unknown_calls=1，未当作 0）。信任链 TEST_ONLY；sandbox none、未配 OBS，均按声明记账。

## Handoff note (written by implementer, ≤ 10 lines)
- 基于已拉取的 main `c76305b`，在 `task/T41-deepseek-harness` 实现；未开 PR、未合并。
- `CliAgent` 支持 `prompt = "arg"`，探测和任务均追加末尾参数并关闭 stdin 输入；默认 stdin 不变。
- 探测明确拒绝 launcher script，JSON `is_error` 优先显示 result；拒绝时不再打印已记账警告。
- 新增 dsh demo，复用 Codex 的任务/验收，显式声明无沙箱和未知 token 用量，GLM 独立审查。
- 原基线 12 项专项通过；新增用例先复现失败，修后 `python -m unittest tests.test_external_agent -v` 15 项通过。
- `python -m unittest discover -s tests -t .`：467 项通过（Windows / Python 3.13.11，9 项跳过）；示例配置与共享验收解析通过。
- `m2k/` 新增 22 行（预算 40）；范围检查通过。真实 dsh 演示按卡留待合并后由用户经 Codex 桌面端运行、Claude 记录。
- CI 和审阅结论待审阅者开 PR 后确认；未将任务状态改为 DONE。
