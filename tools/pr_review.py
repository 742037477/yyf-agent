"""GLM first-pass PR review. Runs in GitHub Actions on every task PR.

Reviews the diff strictly against its task card and AGENTS.md, posts one comment, and exits
non-zero on NO_GO. Only blocking issues inside the card's scope count; everything else is a
non-blocking note, so the reviewer cannot pull the implementer into side quests.
Skips (exit 0) when GLM_API_KEY is not configured. Standard library + m2k only.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from m2k.models import ModelError, ModelReply, OpenAICompatModel as _OpenAICompatModel, extract_json  # noqa: E402
from tools.card_check import find_card  # noqa: E402

DIFF_LIMIT = 80_000

PROMPT = """You are the code reviewer for the m2-kernel project. Review this pull request ONLY against
its task card and the repository rules. You are not the author.

Rules for your review:
- BLOCKING = the card's required behaviour is missing or wrong, a test is missing for new behaviour,
  a bug, a security problem, or a violation of the "Design rules that must never be broken".
- Anything else (style, naming, ideas for later, things outside the card) is a NOTE and never blocks.
- Do not ask for work the card does not require.

=== AGENTS.md ===
{agents}

=== Task card ===
{card}

=== Diff ===
{diff}

Reply with exactly one JSON object and nothing else:
{{"decision": "GO" or "NO_GO", "summary": "one sentence",
  "blocking": ["file:line - problem - what to change", ...], "notes": ["...", ...]}}"""


class OpenAICompatModel(_OpenAICompatModel):
    """PR-only streaming transport; the runtime adapter stays unchanged."""

    def complete(self, messages):
        body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens,
                "stream": True, "stream_options": {"include_usage": True}}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self._key()}"})
        parts, usage = [], {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("usage") is not None:
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if choices:
                        content = (choices[0].get("delta") or {}).get("content")
                        if content is not None:
                            parts.append(content)
                else:
                    raise ModelError("stream ended without [DONE]")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except (OSError, http.client.HTTPException, UnicodeDecodeError) as read_error:
                raise ModelError(f"{type(read_error).__name__}: {read_error}") from read_error
            raise ModelError(f"HTTP {e.code}: {detail}") from e
        except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ModelError(f"{type(e).__name__}: {e}") from e
        text = "".join(parts)
        if not text.strip():
            raise ModelError("empty model reply")
        return ModelReply(text, usage.get("prompt_tokens"), usage.get("completion_tokens"), self.model)


def comment(body: str) -> None:
    repo, pr, token = os.environ["GITHUB_REPOSITORY"], os.environ["PR_NUMBER"], os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues/{pr}/comments", method="POST",
                                 data=json.dumps({"body": body}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    urllib.request.urlopen(req, timeout=30).read()


def _review() -> tuple[str, int]:
    card = find_card(os.environ["PR_TITLE"])
    diff = subprocess.run(["git", "diff", f"origin/{os.environ['BASE_REF']}...HEAD"], cwd=ROOT,
                          check=True, capture_output=True, text=True, encoding="utf-8").stdout
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n...[diff truncated]"
    model = OpenAICompatModel(base_url=os.environ.get("GLM_BASE_URL") or "https://open.bigmodel.cn/api/coding/paas/v4",
                              model=os.environ.get("GLM_MODEL") or "glm-5.3", api_key_env="GLM_API_KEY",
                              max_tokens=32768, timeout_s=120)
    prompt = PROMPT.format(agents=(ROOT / "AGENTS.md").read_text(encoding="utf-8"),
                           card=card.read_text(encoding="utf-8"), diff=diff)
    try:
        reply = model.complete([{"role": "user", "content": prompt}])
        result = extract_json(reply.text)
    except (ModelError, ValueError) as e:
        print(f"review failed: {e}")
        return f"### GLM review: UNKNOWN\nThe review could not be completed: `{e}`. A human/Claude review is required.", 1
    decision = result.get("decision") if result.get("decision") in ("GO", "NO_GO") else "UNKNOWN"
    lines = [f"### GLM review ({model.model}): {decision}", "", str(result.get("summary", ""))]
    if result.get("blocking"):
        lines += ["", "**Blocking (must fix inside this card):**"] + [f"- {b}" for b in result["blocking"]]
    if result.get("notes"):
        lines += ["", "<details><summary>Non-blocking notes (do NOT act on these in this PR)</summary>", ""]
        lines += [f"- {n}" for n in result["notes"]] + ["", "</details>"]
    lines += ["", f"_tokens in/out: {reply.input_tokens}/{reply.output_tokens}. Final acceptance: Claude review._"]
    return "\n".join(lines), 0 if decision == "GO" else 1


def main() -> int:
    if not os.environ.get("GLM_API_KEY"):
        print("GLM_API_KEY not configured; skipping GLM review.")
        return 0
    try:
        body, exit_code = _review()
    except (Exception, SystemExit) as e:
        body = (f"### GLM review: UNKNOWN\nThe review could not be completed: `{type(e).__name__}: {e}`. "
                "A human/Claude review is required.")
        exit_code = 1
    try:
        comment(body)
    except Exception as e:
        print(f"posting review failed: {type(e).__name__}: {e}")
        return 1
    print(body)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
