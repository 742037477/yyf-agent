"""Model adapters. An adapter turns a message list into text plus (if known) token usage.

Adapters have no access to tools, the ledger, or the workspace. Add a provider = add a class.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence


class ModelError(RuntimeError):
    pass


class ConnectionLost(ModelError):
    """The request may have been processed; its token usage is unknown."""


class ProviderBusy(ModelError):
    """The provider refused or never started the request (429/5xx, or an error body without choices): no work was
    done and no tokens were used, so sending it again is safe (T36)."""


BUSY_HTTP = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ModelReply:
    text: str
    input_tokens: int | None = None    # None = provider did not report it
    output_tokens: int | None = None
    model: str = ""
    lost_attempts: int = 0


class ModelAdapter(Protocol):
    name: str

    def complete(self, messages: list[dict[str, str]]) -> ModelReply: ...


class ScriptedModel:
    """Deterministic model for tests and offline demos. Each step is a string or fn(messages)->str."""

    def __init__(self, steps: Sequence[str | Callable[[list[dict]], str]], name: str = "scripted"):
        self.steps = list(steps)
        self.name = name
        self.calls: list[list[dict]] = []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        if not self.steps:
            raise ModelError("scripted model has no more replies")
        step = self.steps.pop(0)
        text = step(messages) if callable(step) else step
        return ModelReply(text, input_tokens=10, output_tokens=10, model=self.name)


class OpenAICompatModel:
    """Any provider exposing POST {base_url}/chat/completions (OpenAI, DeepSeek, Qwen, local vLLM...)."""

    def __init__(self, base_url: str, model: str, api_key_env: str = "OPENAI_API_KEY",
                 max_tokens: int = 4096, temperature: float | None = None, timeout_s: int = 180,
                 extra_body: dict[str, Any] | None = None, name: str | None = None,
                 api_key_file: str | None = None, api_key_field: str | None = None, retry_delays=()):
        self.retry_delays = tuple(float(d) for d in retry_delays)   # seconds before each busy/connection resend
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/chat/completions"
        self.model = model
        self.api_key_env = api_key_env
        self.api_key_file = api_key_file
        self.api_key_field = api_key_field
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.extra_body = extra_body or {}
        self.name = name or f"openai_compat:{model}"

    def _key(self) -> str:
        """Key from api_key_file (+ dotted api_key_field for TOML/JSON files), else from api_key_env."""
        if self.api_key_file:
            return read_secret(self.api_key_file, self.api_key_field)
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise ModelError(f"environment variable {self.api_key_env} is not set")
        return key

    def complete(self, messages):
        key = self._key()
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        body.update(self.extra_body)
        lost_attempts = 0
        for delay in (*self.retry_delays, None):
            try:
                data = self._post(body, key)
                break
            except (ProviderBusy, ConnectionLost) as e:
                if delay is None:
                    raise (type(e)(f"{e} (gave up after {len(self.retry_delays) + 1} attempts)")
                           if self.retry_delays else e) from e.__cause__
                lost_attempts += isinstance(e, ConnectionLost)
                time.sleep(delay)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError(f"unexpected response shape: {str(data)[:300]}") from e
        if choice.get("finish_reason") == "length" and not text.strip():
            raise ModelError("output budget used up before any answer (finish_reason=length)")
        usage = data.get("usage") or {}
        return ModelReply(text, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                          data.get("model", self.model), lost_attempts)

    def _post(self, body: dict[str, Any], key: str) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except (OSError, http.client.HTTPException, UnicodeDecodeError) as read_error:
                raise ModelError(f"{type(read_error).__name__}: {read_error}") from read_error
            raise (ProviderBusy if e.code in BUSY_HTTP else ModelError)(f"HTTP {e.code}: {detail}") from e
        except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError) as e:
            error = (ConnectionLost if isinstance(e, (OSError, http.client.HTTPException)) and not any(
                isinstance(c, ssl.SSLCertVerificationError) for c in (e, getattr(e, "reason", None))) else ModelError)
            raise error(f"{type(e).__name__}: {e}") from e
        if isinstance(data, dict) and "error" in data and "choices" not in data:
            raise ProviderBusy(f"provider error: {str(data['error'])[:300]}")
        return data

    def list_models(self) -> list[str]:
        """GET {base_url}/models — used by `m2k ping` to show valid model ids."""
        req = urllib.request.Request(self.base_url + "/models", headers={"Authorization": f"Bearer {self._key()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise ModelError(f"cannot list models: {e}") from e
        return [m.get("id", "?") for m in data.get("data", []) if isinstance(m, dict)]


def read_secret(path: str, field: str | None) -> str:
    """Read a key from a plain text file, or from a dotted field in a .toml / .json file.
    The value is never logged; errors mention only the path and field name."""
    p = Path(path).expanduser()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ModelError(f"cannot read api_key_file {p}: {type(e).__name__}") from e
    if not field:
        value = raw.strip()
    else:
        try:
            data = tomllib.loads(raw) if p.suffix.lower() == ".toml" else json.loads(raw)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError) as e:
            raise ModelError(f"cannot parse api_key_file {p}") from e
        value = data
        for part in field.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ModelError(f"field '{field}' not found in {p}")
            value = value[part]
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"empty key in {p}" + (f" field '{field}'" if field else ""))
    return value.strip()


def build_model(cfg: dict[str, Any]) -> ModelAdapter:
    kind = cfg.get("kind", "openai_compat")
    if kind == "openai_compat":
        args = {k: v for k, v in cfg.items() if k != "kind"}
        return OpenAICompatModel(**args)
    if kind == "cli":   # an external agent CLI as the worker (T37)
        from .external import CliAgent
        return CliAgent(**{k: v for k, v in cfg.items() if k != "kind"})
    raise ValueError(f"unknown model kind '{kind}'")


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict[str, Any]:
    """Pull one JSON object out of model text (bare, or inside a ```json fence)."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in model reply")
