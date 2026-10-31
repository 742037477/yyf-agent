"""Create a TEST_ONLY deployment, or re-sign its edited configuration. No API calls."""
from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import shutil
import sys
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from m2k import sandbox, trust
from m2k.cli import main as cli_main


def quoted(value):
    return json.dumps(value, ensure_ascii=False)


def cli(*args):
    code = cli_main(list(map(str, args)))
    if code:
        raise ValueError(f"m2k {args[0]} failed (exit {code}); stop and inspect the error above")


def api_model(args, role):
    base, model, env, file = (getattr(args, f"{role}_{field}") for field in
                              ("base_url", "model", "key_env", "key_file"))
    if not base or not model or bool(env) == bool(file):
        raise ValueError(f"--{role}-base-url, --{role}-model and exactly one key source are required")
    result = dict(kind="openai_compat", base_url=base, model=model, max_tokens=32768, timeout_s=600)
    if file:
        path = Path(file).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"--{role}-key-file must name an existing file: {path}")
        result["api_key_file"] = path.as_posix()  # existence only; never read the secret
    else:
        result["api_key_env"] = env
    return result


def worker_model(args):
    if args.worker == "api":
        return api_model(args, "worker")
    if args.worker in ("codex", "claude-code"):
        preset = ("codex_demo/m2k.codex.toml" if args.worker == "codex"
                  else "claude_code_demo/m2k.claude.toml")
        return tomllib.loads((ROOT / "examples" / preset).read_text(encoding="utf-8"))["models"]["worker"]
    try:
        command = json.loads(args.worker_command or "")
    except ValueError:
        raise ValueError("--worker-command must be a non-empty JSON array of non-empty strings") from None
    if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
        raise ValueError("--worker-command must be a non-empty JSON array of non-empty strings")
    return dict(kind="cli", command=command, prompt=args.worker_prompt, timeout_s=1800,
                env=["PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"],
                isolation="No vendor isolation declared; not verified by M2")


def sign(deploy, version, slot="dev"):
    cli("--config", deploy / "m2k.toml", "trust", "sign-release", "--root-key", deploy / "keys/root.pem",
        "--domain", "TEST_ONLY", "--code", "dev", "--slot", slot, "--version", version,
        "--out", deploy / f"keys/release-{version}.json")
    cli("--config", deploy / "m2k.toml", "trust", "status")


def init(args, deploy):
    if deploy.is_relative_to(ROOT) or ROOT.is_relative_to(deploy):
        raise ValueError("deployment must be outside the repository and must not contain it")
    if deploy.exists() and (not deploy.is_dir() or any(deploy.iterdir())):
        raise ValueError("deployment must not exist or must be an empty directory")
    worker, reviewer = worker_model(args), api_model(args, "reviewer")
    worker_name = worker.get("model", "").strip().casefold()
    reviewer_name = reviewer["model"].strip().casefold()
    if (worker_name and worker_name == reviewer_name) or worker.get("name") == f"openai_compat:{reviewer['model']}":
        if not args.allow_same_model:
            raise ValueError("Worker and reviewer use the same model; choose another or pass --allow-same-model")
    if args.allow_same_model:
        reviewer["allow_same_model"] = True
    available, reason = sandbox.Bwrap().available()
    if not available and not args.accept_no_sandbox:
        raise ValueError(f"no usable bubblewrap: {reason}; --accept-no-sandbox is required for unisolated commands")
    deploy.mkdir(parents=True, exist_ok=True)
    output = io.StringIO()
    with redirect_stdout(output):
        cli("trust", "init-dev", "--out", deploy / "keys")
    tables = tomllib.loads("[trust]" + output.getvalue().split("[trust]", 1)[1].split("\n#", 1)[0])
    for table, fields in (("trust", ("root_pin", "charter", "issuer_key", "revocations", "release")),
                          ("obs", ("key", "log"))):
        for field in fields:
            tables[table][field] = Path(tables[table][field]).relative_to(deploy).as_posix()
    tables["trust"]["release"] = "keys/release-1.json"
    sections = {"sandbox": {"backend": "auto" if available else "none"}, **tables,
                "models.worker": worker, "models.reviewer": reviewer}
    config = 'ledger = "state/ledger.sqlite"\n'
    for table, values in sections.items():
        config += f"\n[{table}]\n" + "".join(f"{k} = {quoted(v)}\n" for k, v in values.items())
    (deploy / "m2k.toml").write_text(config, encoding="utf-8")
    sign(deploy, 1)
    shutil.copytree(ROOT / "examples/demo_calc", deploy / "tasks/demo_calc",
                    ignore=shutil.ignore_patterns("ws", "out", "__pycache__", "*.pyc"))
    print("TEST_ONLY deployment ready. Set the named key environment variables, or keep the key files in place.")
    print("From the repository root, run:")
    def shell_path(path):
        text = path.as_posix()
        return "'" + text.replace("'", "''") + "'" if sys.platform == "win32" else shlex.quote(text)

    prefix = f"python -m m2k --config {shell_path(deploy / 'm2k.toml')}"
    for command in ("ping", f"run {shell_path(deploy / 'tasks/demo_calc/task.toml')}", "status calc-001",
                    f"check-delivery {shell_path(deploy / 'tasks/demo_calc/out')}"):
        print(f"{prefix} {command}")


def resign(deploy):
    path = deploy / "m2k.toml"
    original_bytes = path.read_bytes()
    original = original_bytes.decode("utf-8")
    cfg = tomllib.loads(original)
    section = cfg["trust"]
    if section["domain"] != "TEST_ONLY":
        raise ValueError("quickstart only supports TEST_ONLY deployments")
    table = re.search(r"(?m)^[ \t]*\[trust\][ \t]*(?:#[^\r\n]*)?\r?$", original)
    if table is None:
        raise ValueError('[trust] release: 请手工改成单行写法 release = "..."')
    tail = original[table.end():]
    end = re.search(r"(?m)^[ \t]*\[", tail)
    stop = table.end() + (end.start() if end else len(tail))
    pattern = (r'''(?m)^[ \t]*release[ \t]*=[ \t]*("(?:\\[^\r\n]|[^"\\\r\n])*"|'[^'\r\n]*')'''
               r'''[ \t]*(?:#[^\r\n]*)?\r?$''')
    assignment = re.search(pattern, original[table.end():stop])
    if assignment is None:
        raise ValueError('[trust] release: 请手工改成单行写法 release = "..."')
    body = trust.verify_release(trust.load_pin(deploy / section["root_pin"]), "TEST_ONLY",
                                trust.read_json(deploy / section["release"]))
    version = body["version"] + 1
    release = f"keys/release-{version}.json"
    if (deploy / release).exists():
        raise ValueError(f"output exists: {deploy / release}")
    start, stop = (table.end() + offset for offset in assignment.span(1))
    updated = original[:start] + quoted(release) + original[stop:]
    parsed = tomllib.loads(updated)
    if parsed != {**cfg, "trust": {**section, "release": release}}:
        raise ValueError('[trust] release replacement changed other values; 请手工改成单行写法')
    succeeded = False
    try:
        path.write_bytes(updated.encode("utf-8"))
        sign(deploy, version, body["slot"])
        succeeded = True
    finally:
        if not succeeded:
            path.write_bytes(original_bytes)
            (deploy / release).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("init")
    p.add_argument("deploy", type=Path)
    p.add_argument("--worker", choices=("api", "codex", "claude-code", "cli"), required=True)
    for role in ("worker", "reviewer"):
        p.add_argument(f"--{role}-base-url")
        p.add_argument(f"--{role}-model")
        group = p.add_mutually_exclusive_group(required=role == "reviewer")
        group.add_argument(f"--{role}-key-env")
        group.add_argument(f"--{role}-key-file")
    p.add_argument("--worker-command")
    p.add_argument("--worker-prompt", choices=("stdin", "arg"), default="stdin")
    p.add_argument("--allow-same-model", action="store_true")
    p.add_argument("--accept-no-sandbox", action="store_true")
    sub.add_parser("resign").add_argument("deploy", type=Path)
    args = parser.parse_args(argv)
    try:
        deploy = args.deploy.expanduser().resolve()
        if args.action == "init":
            init(args, deploy)
        else:
            resign(deploy)
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"quickstart: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
