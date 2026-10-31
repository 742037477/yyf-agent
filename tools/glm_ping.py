"""Check which GLM endpoint accepts GLM_API_KEY (pay-as-you-go vs Coding Plan). Never prints the key."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from m2k.models import ModelError, OpenAICompatModel  # noqa: E402

ENDPOINTS = {"pay-as-you-go": "https://open.bigmodel.cn/api/paas/v4",
             "coding-plan": "https://open.bigmodel.cn/api/coding/paas/v4"}
if not os.environ.get("GLM_API_KEY"):
    sys.exit("GLM_API_KEY secret is not available to this workflow")
model = os.environ.get("GLM_MODEL") or "glm-5.3"
ok = []
for label, url in ENDPOINTS.items():
    m = OpenAICompatModel(url, model, api_key_env="GLM_API_KEY", max_tokens=2048, timeout_s=120)
    try:
        r = m.complete([{"role": "user", "content": 'Reply with exactly: {"ok": true}'}])
        print(f"OK   {label:<14} {url}  model={r.model} tokens={r.input_tokens}/{r.output_tokens} reply={r.text[:60]!r}")
        ok.append(label)
    except ModelError as e:
        print(f"FAIL {label:<14} {url}  {str(e)[:300]}")
print(f"::notice title=GLM endpoint check::working endpoints: {', '.join(ok) or 'none'}")
sys.exit(0 if ok else 1)
