# Deploy m2k and integrate an agent harness

This checklist creates a **TEST_ONLY** trust chain for a first local task. It is not a production deployment.
Read [POSITIONING](POSITIONING.md) for the audited scope and experimental limits.

## Ask the human first

Obtain these decisions before choosing defaults. Reuse decisions already given in the current session.

- Worker: OpenAI-compatible API, Codex, Claude Code, or a custom non-interactive CLI; API URL and model if applicable.
- Reviewer: OpenAI-compatible API URL and model. Prefer a different model/provider; same-model review requires explicit consent.
- Credentials: environment variable names or existing UTF-8 plain-text key file paths. Never ask the human to paste a key into chat.
- Whether unisolated commands are acceptable if working bubblewrap is unavailable (`--accept-no-sandbox`).
- Deployment location: absent or empty, outside the repository and every task workspace.

## Hard rules

- Never print, copy, log or commit API keys or anything under `keys/`. Refer to paths only; do not dump config/credential files.
- Never run `m2k run --untrusted`.
- Never edit `m2k.toml` without running `quickstart.py resign` afterwards. Do not silently change a signed configuration.
- Never put the deployment folder or ledger inside a task workspace.
- Stop and report the exact error text instead of working around a refusal. Redact secrets if an external provider echoes them.

## Executable checklist

Run commands from the repository root. `python` must resolve to Python 3.11 or newer.
Keep this checkout available; the commands below do not require installing m2k itself.

1. Check the environment and obtain the code.

   ```text
   git clone https://github.com/742037477/yyf-agent
   cd yyf-agent
   python --version
   git --version
   python -m pip install cryptography
   ```

   Success: all commands exit 0; Python is >=3.11; `tools/quickstart.py` exists.
   If the checkout is already provided, verify it instead of cloning over it.
   On Windows, select a suitable interpreter with
   `Set-Alias python 'C:/path/to/python.exe'` in PowerShell; replace the placeholder with its actual path.

2. Set a deployment path chosen by the human. PowerShell:

   ```powershell
   $env:PYTHONUTF8 = '1'
   $DEPLOY = Join-Path $env:USERPROFILE 'm2k-deploy'
   ```

   bash:

   ```bash
   export PYTHONUTF8=1
   DEPLOY="$HOME/m2k-deploy"
   ```

   Initialize once. This complete API example uses named environment variables:

   ```text
   python tools/quickstart.py init "$DEPLOY" --worker api --worker-base-url https://api.deepseek.com/v1 --worker-model deepseek-flash --worker-key-env DEEPSEEK_API_KEY --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
   ```

   Include `--accept-no-sandbox` only if the human accepted it; omit it with working Linux bubblewrap.
   Replace each `--ROLE-key-env NAME` with `--ROLE-key-file /absolute/existing/key.txt` when the human selected a file.
   Do not open that file. The helper checks existence and stores its absolute path, without reading the contents.
   For Codex replace the worker API options with `--worker codex`; for Claude Code use `--worker claude-code`.
   Their CLIs must already be installed, on PATH and authenticated by the human.
   For a custom harness, replace the API init above with the matching shell command below, using a fresh `$DEPLOY`.
   Windows PowerShell 5.1 (keep the backslashes before the JSON double quotes):

   ```powershell
   python tools/quickstart.py init "$DEPLOY" --worker cli --worker-command '[\"my-agent\", \"--non-interactive\"]' --worker-prompt stdin --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
   ```

   bash:

   ```bash
   python tools/quickstart.py init "$DEPLOY" --worker cli --worker-command '["my-agent", "--non-interactive"]' --worker-prompt stdin --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
   ```

   Both forms were tested locally: init exited 0 in Windows PowerShell 5.1 and Git bash, with an unset key variable and no model calls.
   Install/authenticate your own `my-agent` and set the reviewer key before ping/run.
   Keep the reviewer options in every case. Add `--allow-same-model` only if the human explicitly chose same-model review.

   Success: exit 0; output includes `slot dev version 1`, `release manifest: ok`, `obs: ok`,
   and `TEST_ONLY deployment ready.`. The demo contract is at `$DEPLOY/tasks/demo_calc/task.toml`.
   Never rerun init on an existing deployment; a nonempty directory is refused with exit 2.

3. Have the human set the named environment variables in the shell used to run m2k.
   Hidden-input PowerShell and bash commands are in [SETUP step 3](SETUP.md#3-提供-key并确认连接).
   File-based credentials need no environment variable. Do not verify credentials by printing them.

   ```text
   python -m m2k --config "$DEPLOY/m2k.toml" trust status
   python -m m2k --config "$DEPLOY/m2k.toml" ping
   ```

   Success: trust status exits 0 and includes `release manifest: ok`, `obs: ok` and a TEST_ONLY Root fingerprint.
   Initially `obs root: not yet recorded` is expected; after a run it should be `obs root: ok`.
   Ping makes a small paid API request per role, or launches a CLI probe. Require exit 0 **and both role lines to say OK**.
   CLI success includes `external agent` and `answered`.

4. Run the supplied demo contract without changing its acceptance test.

   ```text
   python -m m2k --config "$DEPLOY/m2k.toml" run "$DEPLOY/tasks/demo_calc/task.toml"
   python -m m2k --config "$DEPLOY/m2k.toml" status calc-001
   ```

   Success: run exits 0, prints `calc-001: PASS` and `delivered to:`; status includes a PASS verdict.
   A warning about `backend = "none"` is expected only when unisolated commands were accepted.
   FAIL and UNKNOWN do not deliver. Exit 3 means awaiting the owner; stop and report it.
   IDs cannot be reused. A new run requires a new contract ID and a fresh workspace; do not erase ledger events.

5. Verify the actual delivery and report to the human.

   ```text
   python -m m2k --config "$DEPLOY/m2k.toml" check-delivery "$DEPLOY/tasks/demo_calc/out"
   ```

   Success: exit 0, `OK calc-001 (2 files)` and `合计 1 份：通过 1 份，未经核验 0 份`.
   An empty root can exit 0 with `OK empty delivery root`; that does **not** satisfy this checklist.
   Report the deployment/config/ledger/delivery paths, run verdict and verification result. Do not report keys or private file contents.

After any authorized configuration edit:

```text
python tools/quickstart.py resign "$DEPLOY"
python -m m2k --config "$DEPLOY/m2k.toml" trust status
```

Success: both exit 0, a higher `slot dev version N` is printed, and `release manifest: ok` appears.
Only the `[trust] release` value is replaced; old manifests remain. This helper supports TEST_ONLY only.
Use a single-line `release = "..."` or `release = '...'` inside `[trust]`; other forms exit 2 with “请手工改成单行写法” without editing the config.
If signing or trust status fails, the original config bytes are restored and the new manifest is removed; fix the cause and rerun resign.

## Integrating your own agent harness

The worker can be a CLI process; the reviewer remains an API model. A minimal worker table is:

```toml
[models.worker]
kind = "cli"
name = "my-harness"
command = ["my-agent", "--non-interactive"]
prompt = "stdin"
env = ["PATH", "SYSTEMROOT", "TEMP", "TMP"]
timeout_s = 300
isolation = "Vendor-declared isolation, not verified by M2"
```

- Run non-interactively, read the task prompt from stdin (default), finish the work, and exit normally with code 0.
  With `prompt = "arg"`, the entire prompt is appended as the final command argument instead.
- The current directory is the task workspace. Leave deliverable files there and preserve the owner's protected acceptance files.
  A shell command string is not the command format: use a JSON/TOML array of executable plus arguments.
- `env` is an allowlist. Include only necessary variables; do not include the reviewer's credentials or m2k secrets.
  Add vendor login/config variables only if needed. An allowlist does not prevent a same-user process from reading files.
- Only `usage = "codex-jsonl"` and `usage = "claude-json"` are parsed for token accounting.
  Other output formats remain **unknown**, not zero; omit `usage` for them. The token budget cannot constrain unreported usage;
  use `timeout_s` and external billing limits. m2k cannot observe individual actions inside an external agent.
- Optional `.m2k/ask.json` requests a question, scope increase or budget increase, then the process exits normally.
  The format is documented in [README T79](../README.md#挂起与所有者批准t79); a request does not itself grant permission.
- `isolation` records the vendor's claim; m2k does not verify it. Linux command bubblewrap does not wrap the external CLI itself.
- `m2k ping` probes the CLI with a trivial prompt first; `run` also probes before task submission.
  Fix startup/authentication/argument errors with the human, then resign any config edit and repeat the check.

## What m2k does not guarantee

- TEST_ONLY keys and a same-user deployment do not protect the private OBS key from a malicious agent.
- PASS binds the frozen candidate to the supplied acceptance and independent review, not to every unstated requirement.
- An independent model call can still judge incorrectly; the experiments did not establish an AI-review advantage.
- Unlisted file reads, exfiltration and irreversible external effects are not covered by these local checks.

See [POSITIONING](POSITIONING.md#3-不管什么--证明不了) before making stronger claims.
