"""Workspace tools. These are the ONLY code paths that touch the outside world for a worker.

Tools never check permissions themselves; gates do that before `execute` is called.
Paths are still confined to the workspace here as a second line of defence.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .types import Effect, EffectStatus

MAX_READ = 20_000
MAX_OUTPUT = 8_000
MAX_LIST = 300
COMMAND_TIMEOUT_CAP = 600
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".m2k", ".venv", "node_modules"}
# Key guard: commands never inherit variables whose NAME looks secret, and any such value that
# still shows up in command output is replaced before it reaches the ledger or the model.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|PRIVATE", re.I)
COMMAND_USER_ENV = "M2K_COMMAND_USER"   # POSIX + root only: run every command as this unprivileged user
REDACTED = "[REDACTED]"


class ScopeError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path, sandbox=None, network: bool = False):
        self.root = Path(root).resolve()
        self.sandbox, self.network = sandbox, network   # how commands run here (m2k/sandbox.py)

    def resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ScopeError("path must be a non-empty string")
        p = Path(rel)
        target = (p if p.is_absolute() else self.root / p).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ScopeError(f"path escapes workspace: {rel}")
        return target

    def files(self) -> list[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                out.append(Path(dirpath) / name)
        return out


def expand_python(command: str) -> str:
    exe = sys.executable
    quoted = f'"{exe}"' if " " in exe else exe
    return command.replace("{python}", quoted)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the shell AND everything it started, so no orphan keeps running or holds the pipe."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def command_env() -> tuple[dict[str, str], list[str]]:
    """Environment for every command, and the secret values it withheld (for output redaction)."""
    env, secrets = {}, []
    for name, value in os.environ.items():
        if SECRET_NAME.search(name):
            if len(value) >= 8:
                secrets.append(value)
        elif name != COMMAND_USER_ENV:
            env[name] = value
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env, sorted(secrets, key=len, reverse=True)


def _as_user(env: dict[str, str]) -> dict:
    """Popen arguments that drop to M2K_COMMAND_USER. Fails closed: misconfiguration raises."""
    name = os.environ.get(COMMAND_USER_ENV, "").strip()
    if not name:
        return {}
    if os.name == "nt":
        raise OSError(f"{COMMAND_USER_ENV} is not supported on Windows")
    import pwd
    pw = pwd.getpwnam(name)
    if pw.pw_uid == 0 or os.geteuid() != 0:
        raise OSError(f"{COMMAND_USER_ENV} needs m2k running as root and a non-root target user")
    env.update(HOME=pw.pw_dir, USER=name, LOGNAME=name)
    return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}


def redact(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, REDACTED)
    return text


def run_shell(command: str, cwd: Path, timeout_s: int, extra_env: dict[str, str] | None = None,
              sandbox=None, network: bool = False) -> Effect:
    env, secrets = command_env()
    env.update(extra_env or {})
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    r_fd, w_fd = os.pipe() if sandbox is not None and os.name != "nt" else (None, None)
    try:
        user = _as_user(env)
        argv = sandbox.argv(expand_python(command), cwd, network, w_fd) if sandbox else None
        shell = {"args": expand_python(command), "shell": True} if argv is None else {"args": argv, "pass_fds": (w_fd,)}
        proc = subprocess.Popen(**shell, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                **group, **user)
    except (OSError, KeyError) as e:
        _readable(r_fd)
        return Effect(EffectStatus.FAILED, str(e), {"reason": "START_FAILED"})
    finally:
        if w_fd is not None:
            os.close(w_fd)
    try:
        raw, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raw = b""
        # We do not know what the command did before it was killed.
        return Effect(EffectStatus.UNKNOWN, _tail(redact(raw.decode("utf-8", "replace"), secrets)),
                      {"reason": "TIMEOUT", "timeout_s": timeout_s})
    finally:
        started = _readable(r_fd) or argv is None
    out = redact(raw.decode("utf-8", "replace"), secrets)
    if not started:   # the sandbox never came up: the command did not run, isolated or otherwise
        return Effect(EffectStatus.FAILED, _tail(out), {"reason": "START_FAILED", "exit_code": proc.returncode})
    status = EffectStatus.COMPLETED if proc.returncode == 0 else EffectStatus.FAILED
    return Effect(status, _tail(out), {"exit_code": proc.returncode})


def _readable(fd: int | None) -> bool:
    """True if the backend wrote its 'sandbox is up' report; always closes fd (None: no pipe, e.g. Windows)."""
    if fd is None:
        return False
    try:
        os.set_blocking(fd, False)
        return bool(os.read(fd, 4096))
    except OSError:   # includes BlockingIOError: nothing was written
        return False
    finally:
        os.close(fd)


def _tail(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]


# ---- tool implementations ----------------------------------------------------
def _list_files(ws: Workspace, args: dict) -> Effect:
    base = ws.resolve(args.get("path", "."))
    if not base.exists():
        return Effect(EffectStatus.FAILED, f"not found: {args.get('path', '.')}")
    rels = [p.relative_to(ws.root).as_posix() for p in ws.files() if p.is_relative_to(base)]
    shown = rels[:MAX_LIST]
    extra = f"\n...and {len(rels) - MAX_LIST} more" if len(rels) > MAX_LIST else ""
    return Effect(EffectStatus.COMPLETED, ("\n".join(shown) or "(empty)") + extra, {"count": len(rels)})


def _read_file(ws: Workspace, args: dict) -> Effect:
    p = ws.resolve(args["path"])
    if not p.is_file():
        return Effect(EffectStatus.FAILED, f"not a file: {args['path']}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ:
        text = text[:MAX_READ] + f"\n...[truncated, {len(text)} chars total]"
    return Effect(EffectStatus.COMPLETED, text)


def _write_file(ws: Workspace, args: dict) -> Effect:
    p = ws.resolve(args["path"])
    content = args["content"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    return Effect(EffectStatus.COMPLETED, f"wrote {len(content)} chars to {args['path']}",
                  {"bytes": len(content.encode("utf-8"))})


def _edit_file(ws: Workspace, args: dict) -> Effect:
    p = ws.resolve(args["path"])
    old, new = args["old"], args["new"]
    if not old:
        return Effect(EffectStatus.FAILED, "old must be non-empty")
    if not p.is_file():
        return Effect(EffectStatus.FAILED, f"not a file: {args['path']}")
    text = p.read_text(encoding="utf-8")
    count, pos = 0, text.find(old)
    while pos != -1:
        count += 1
        pos = text.find(old, pos + 1)
    if count != 1:
        return Effect(EffectStatus.FAILED, f"found {count} occurrences of old; expected exactly 1")
    p.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    return Effect(EffectStatus.COMPLETED,
                  f"edited {args['path']}: replaced 1 occurrence "
                  f"(-{len(old.splitlines())} +{len(new.splitlines())} lines)")


def _run_command(ws: Workspace, args: dict) -> Effect:
    timeout = min(int(args.get("timeout_s", 120)), COMMAND_TIMEOUT_CAP)
    return run_shell(args["command"], ws.root, timeout, sandbox=ws.sandbox, network=ws.network)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, str]          # name -> "type, description"; all str/int params
    required: tuple[str, ...]
    fn: Callable[[Workspace, dict], Effect]
    path_params: tuple[str, ...] = ()


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("list_files", "List files under a workspace folder (recursive).",
             {"path": "string, folder relative to workspace, default '.'"}, (), _list_files, ("path",)),
    ToolSpec("read_file", "Read a UTF-8 text file.",
             {"path": "string, file path relative to workspace"}, ("path",), _read_file, ("path",)),
    ToolSpec("write_file", "Create or overwrite a UTF-8 text file with the full content.",
             {"path": "string, file path relative to workspace", "content": "string, full file content"},
             ("path", "content"), _write_file, ("path",)),
    ToolSpec("edit_file", "Replace exactly one occurrence of non-empty old text in a UTF-8 file.",
             {"path": "string, file path relative to workspace", "old": "string, exact text to replace",
              "new": "string, replacement text"}, ("path", "old", "new"), _edit_file, ("path",)),
    ToolSpec("run_command", "Run a shell command in the workspace root. Use {python} for the Python interpreter.",
             {"command": "string", "timeout_s": "integer seconds, default 120"}, ("command",), _run_command),
]}

FINISH = ToolSpec("finish", "Declare the task done. The system will then run the acceptance check and an independent review.",
                  {"summary": "string, what you did"}, ("summary",), lambda ws, a: Effect(EffectStatus.COMPLETED, ""))


def describe(tool_names: tuple[str, ...]) -> str:
    lines = []
    for name in (*tool_names, "finish"):
        spec = FINISH if name == "finish" else TOOLS[name]
        params = ", ".join(f"{k} ({v})" for k, v in spec.params.items())
        lines.append(f"- {spec.name}: {spec.description} Params: {params or 'none'}. Required: {list(spec.required)}")
    lines.append('- ask_owner: Stop and ask the owner. Required: kind (question, scope, budget), text (1–2000 chars). '
                 'Optional request: scope {writes: [paths], tools: [names], network: bool}; '
                 'budget {budget: {max_rounds, max_model_calls, max_tokens, max_commands: integers}}.')
    return "\n".join(lines)


def execute(ws: Workspace, tool: str, args: dict[str, Any]) -> Effect:
    spec = TOOLS[tool]
    try:
        return spec.fn(ws, args)
    except ScopeError as e:
        return Effect(EffectStatus.FAILED, str(e), {"reason": "SCOPE"})
    except (OSError, ValueError, KeyError) as e:
        return Effect(EffectStatus.FAILED, f"{type(e).__name__}: {e}")
