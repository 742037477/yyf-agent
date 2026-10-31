"""Command line entry: m2k run | status | log | report | check-ledger | demo"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
import tomllib
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path

from . import anchor, obs, sandbox as sandboxes, trust
from .adjudicate import amendment_basis, approval_needs
from .contract import ContractError, load_contract, parse_contract
from .delivery import check_delivery, delivery_entries
from .external import overlaps, watch_snapshot
from .fingerprint import framework_fingerprint
from .kernel import Kernel, KernelError, RunReport, recall, status
from .ledger import Ledger
from .models import build_model

DEFAULT_CONFIG = "m2k.toml"


def _default_ledger() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "m2k" / "ledger.sqlite"


def _load_config(path: str | None) -> tuple[dict, Path, bytes]:
    p = Path(path or DEFAULT_CONFIG)
    if not p.exists():
        if path:
            raise SystemExit(f"config not found: {p}")
        return {}, Path.cwd(), b""
    raw = p.read_bytes()
    return tomllib.loads(raw.decode("utf-8")), p.resolve().parent, raw


def _ledger_path(args, config=None) -> Path:
    cfg, base, _ = config if config is not None else _load_config(args.config)
    raw = args.ledger or cfg.get("ledger")
    p = _default_ledger() if raw is None else Path(raw)
    if raw is None and args.cmd in ("status", "log", "report", "check-ledger", "adjudicate", "live") and Path(".m2k/ledger.sqlite").is_file():
        print("旧账本仍在 .m2k/ledger.sqlite；查看旧任务请加 --ledger .m2k/ledger.sqlite", file=sys.stderr)
    return p if p.is_absolute() else base / p


def _ledger(args, config=None) -> Ledger:
    return Ledger(_ledger_path(args, config))


def _startup_refused(args, config, task, code, reason) -> int:
    try:
        with Ledger(_ledger_path(args, config)) as ledger:
            ledger.append("m2k.startup", "startup.refused", {"task": task, "code": code, "reason": reason})
    except (OSError, sqlite3.Error) as e:
        print(f"（账本打不开，本次拒绝未记账：{e}）", file=sys.stderr)
    return 2


def print_report(r: RunReport) -> None:
    if r.awaiting:
        print(f"⏸ 待授权（{r.awaiting['kind']}）：{r.awaiting['text']}")
        print(f"下一步：m2k answer {r.task_id} <新契约.toml>")
    mark = {"PASS": "✅", "FAIL": "❌", "UNKNOWN": "❔"}[r.reported_verdict.value]
    print(f"\n{mark} {r.task_id}: {r.reported_verdict.value}")
    if r.reported_verdict is not r.verdict:
        print(f"{mark} {r.task_id}: 未交付（内核判定 {r.verdict.value}，{r.reasons[-1]}）")
    print(f"worker: {r.worker.status.value} — {r.worker.reason}")
    if r.worker.summary:
        print(f"worker summary: {r.worker.summary}")
    for line in r.reasons:
        print(f"  {line}")
    if r.test is not None and r.test.verdict.value != "PASS" and r.test.output:
        print("acceptance output (tail):\n" + r.test.output[-1500:])
    if r.delivered_to:
        print(f"delivered to: {r.delivered_to}")
    print("usage: " + json.dumps(r.usage, ensure_ascii=False))


def cmd_run(args) -> int:
    if args.cmd == "answer" and not sys.stdin.isatty():
        print("m2k answer 只能由所有者在交互终端里运行", file=sys.stderr)
        return 2
    with ExitStack() as cleanup:
        try:
            return _run(args, cleanup)
        except (KernelError, OSError, ValueError) as e:
            raise SystemExit(str(e))


def _amendment_state(ledger, task_id, contract):
    try:
        _, _, version, _ = amendment_basis(ledger, task_id)
    except ValueError as e:
        reason = next((zh for en, zh in (("amended task missing", "任务不存在"), ("has a parent", "子任务不能修订"),
                       ("has no contract", "任务没有契约"), ("has no verdict", "任务尚无判定"),
                       ("has no frozen candidate", "任务没有冻结候选"), ("already amended", "任务已有后继版本"))
                       if en in str(e)), "修订记录无效")
        raise ValueError(f"无法修订任务 {task_id}：{reason}（{e}）") from e
    return version, approval_needs(ledger, task_id, json.loads(json.dumps(contract.data(), default=str)))


def _run(args, cleanup) -> int:
    cfg, base, raw = _load_config(args.config)
    models = cfg.get("models", {})
    if "worker" not in models or "reviewer" not in models:
        raise SystemExit("config needs [models.worker] and [models.reviewer]; see m2k.example.toml")
    if not isinstance(cfg.get("sandbox", {}), dict):
        print('配置错误：[sandbox] 必须是一个表，例如 [sandbox] backend = "bwrap"', file=sys.stderr)
        return 2
    if any(not isinstance(models[role].get("model", ""), str) for role in ("worker", "reviewer")):
        print("配置错误：[models.worker] 和 [models.reviewer] 的 model 必须是字符串", file=sys.stderr)
        return 2
    allow_same_model = models["reviewer"].get("allow_same_model", False)
    if not isinstance(allow_same_model, bool):
        print("配置错误：[models.reviewer] 的 allow_same_model 必须是布尔值（true 或 false）", file=sys.stderr)
        return 2
    try:
        if getattr(args, "amends", None):
            path = Path(args.task).resolve()
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            parent = parse_contract({k: v for k, v in data.items() if k != "subtasks"}, path.parent)
            data["budget"] = dict.fromkeys(asdict(parent.budget))  # defer budget sums until the impact is known
            contract = replace(parse_contract(data, path.parent), budget=parent.budget)
        else:
            contract = load_contract(args.task)
    except (ContractError, OSError, tomllib.TOMLDecodeError) as e:
        raise SystemExit(f"invalid task contract: {e}")
    approval = None
    if args.cmd == "answer":
        text = args.text
        if text is not None:
            if not text.strip() or len(text) > 2000:
                print("--text 必须是非空字符串，最多 2000 字符", file=sys.stderr)
                return 2
            contract = replace(contract, constraints=contract.constraints + (f"Owner's answer: {text}",))
        with _ledger(args, (cfg, base, raw)) as ledger:
            version, needs = _amendment_state(ledger, args.amends, contract)
            print(f"修订任务：{args.amends}；新版本：{version}")
            for seq in needs["requests"]:
                e = ledger.event(seq)
                print(f"待答请求：序号 {seq}；任务 {e.task_id}；" + json.dumps(e.payload, ensure_ascii=False))
            for section, label in (("scope", "范围"), ("budget", "预算")):
                for key, (old, new) in needs[section].items():
                    print(f"{label} {key}：{old} → {new}")
        print(f"所有者回答：{text if text is not None else '（无）'}")
        try:
            confirmed = input("输入 YES 确认（其他任何输入取消）：") == "YES"
        except EOFError:
            confirmed = False
        if not confirmed:
            print("已取消")
            return 1
        approval = {"by": "m2k answer", "text": text, "needs": needs}
    if args.amends:
        with _ledger(args, (cfg, base, raw)) as ledger:
            _, needs = _amendment_state(ledger, args.amends, contract)
        if approval is not None and approval["needs"] != needs:
            raise ValueError("拒绝修订：确认后情况已改变，请重新运行 m2k answer")
        if any(needs.values()) and approval is None:
            raise ValueError(f"拒绝修订：以下内容需要所有者批准 {needs}；请在交互终端运行 m2k answer")
    def refused(code, reason):
        return _startup_refused(args, (cfg, base, raw), contract.task_id, code, reason)
    try:
        section = cfg.get("watch", {})
        if not isinstance(section, dict):
            raise ValueError("[watch] 必须是一个表")
        values = section.get("paths", [])
        if not isinstance(values, list) or any(not isinstance(p, str) or not p.strip() for p in values):
            raise ValueError("paths 必须是由非空字符串组成的列表")
        watch = [Path(os.path.abspath(base / Path(p).expanduser())) for p in values]
        if watch:
            busy = [_ledger_path(args, (cfg, base, raw)), _anchor_path(args, config=(cfg, base, raw))]
            if "obs" in cfg:
                busy += [base / cfg["obs"]["key"], (base / cfg["obs"]["log"]).parent]
            if cfg.get("anchor", {}).get("repo_dir"):
                busy.append(base / cfg["anchor"]["repo_dir"])
            if any(not p.exists() and not p.is_symlink() for p in watch):
                raise ValueError("监视路径不存在")
            if any(overlaps(p, b) for p in watch for b in busy if b is not None):
                raise ValueError("监视路径与账本、OBS 或锚点的运行文件或目录重叠")
            if watch_snapshot(watch)[1]:
                raise ValueError("监视路径无法完整读取，或总文件数超过 20000")
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
        reason = f"配置错误：[watch] {e}"
        print(reason, file=sys.stderr)
        return refused("CONFIG", reason)
    if "trust" in cfg and args.untrusted:
        print("已配置 [trust]，不能同时用 --untrusted", file=sys.stderr)
        return refused("CONFIG", "已配置 [trust]，不能同时用 --untrusted")
    if "trust" not in cfg:
        if not args.untrusted:
            reason = ("拒绝启动：没有信任链（[trust]）。两个选择：1) 运行 m2k trust init-dev --out keys 生成测试链，"
                      "并把它打印的 [trust] 段加进 m2k.toml；2) 只这一次不授权运行：m2k run --untrusted <task>（会记入账本）。")
            print(reason, file=sys.stderr)
            return refused("NO_TRUST", reason)
    code_dir = Path(trust.__file__).resolve().parent
    config_path = Path(args.config or DEFAULT_CONFIG).absolute()
    contents = {config_path: raw}
    if "trust" in cfg:
        section = cfg["trust"] if isinstance(cfg["trust"], dict) else {}
        try:
            contents.update(trust.check_release(section, base, code_dir, raw))
        except trust.TrustError as e:
            print(f"拒绝启动：发行清单核对失败（{e.code}）：{e}", file=sys.stderr)
            print(f"若此变更由所有者作出，请设置 [trust] release 后重签：m2k --config {json.dumps(args.config or DEFAULT_CONFIG, ensure_ascii=False)} "
                  f"trust sign-release --root-key <Root.pem> --domain {section.get('domain', 'TEST_ONLY')} "
                  "--slot <slot> --version <N> --out <release.json>（TEST_ONLY 可加 --code dev；已有清单请另选新路径）。", file=sys.stderr)
            return refused(e.code, str(e))
        if "obs" not in cfg:
            reason = "拒绝启动：配置了信任链但没有 OBS 独立旁证（[obs]）。"
            print(reason, file=sys.stderr)
            print("运行 m2k obs keygen --out <dir>，加入打印的 [obs] 表，再用 m2k trust sign-release 重签发行清单。", file=sys.stderr)
            return refused("OBS_REQUIRED", reason)
    observer, section = None, cfg.get("obs")
    if section is None:
        print("警告：未配置 OBS 独立旁证，交付只凭账本裁决。", file=sys.stderr)
    else:
        try:
            if not isinstance(section["public_key_b64"], str):
                raise TypeError("public_key_b64 must be a string")
            key, log = ((base / section[k]).resolve() for k in ("key", "log"))
            if any(p.is_relative_to(contract.workspace.resolve()) for p in (key, log)):
                raise KernelError("OBS key or log is inside the workspace")
            if trust.public_key_b64(trust.load_private_key(key)) != section["public_key_b64"]:
                raise KernelError("OBS key does not match public_key_b64")
            observer = obs.ObsClient([sys.executable, "-m", "m2k", "obs", "serve", "--key", str(key), "--log", str(log)],
                                     section["public_key_b64"], log_path=log)
            cleanup.callback(observer.close)
        except (KeyError, TypeError) as e:
            print(f"配置错误：[obs] {e}", file=sys.stderr)
            return refused("OBS_CONFIG", f"配置错误：[obs] {e}")
        except KernelError as e:
            refused("OBS_CONFIG", str(e))
            raise SystemExit(str(e))
        except obs.ObsError as e:
            print(f"拒绝启动：OBS_UNAVAILABLE: {e}", file=sys.stderr)
            return refused("OBS_UNAVAILABLE", str(e))
    path = _anchor_path(args, config=(cfg, base, raw))
    repo = base / cfg["anchor"]["repo_dir"] if cfg.get("anchor", {}).get("repo_dir") else None
    workspace = contract.workspace.resolve()
    workspaces = (workspace, workspace.parent / f"{workspace.name}.subtasks")
    if any(p.resolve().is_relative_to(w) for p in (path, repo) if p for w in workspaces):
        refused("ANCHOR_CONFIG", "anchor file or repo-dir is inside the workspace")
        raise SystemExit("anchor file or repo-dir is inside the workspace")
    production = cfg.get("trust", {}).get("domain") == "PRODUCTION"
    if repo is not None or production:
        section = cfg.get("obs", {})
        with _ledger(args, (cfg, base, raw)) as ledger:
            problems = (["PRODUCTION requires [anchor] with repo_dir"] if repo is None else
                        anchor.remote_check(path, ledger, repo, section.get("public_key_b64"),
                                            base / section["log"] if section.get("log") else None))
            if problems:
                if production:
                    code = "ANCHOR_REQUIRED" if repo is None else "ANCHOR_CHECK"
                    reason = "; ".join(problems)
                    print(f"拒绝启动：{code}: {reason}", file=sys.stderr)
                    ledger.append("m2k.startup", "startup.refused", {"task": contract.task_id, "code": code, "reason": reason})
                    return 2
                ledger.append("m2k.startup", "anchor.warning", {"task": contract.task_id, "problems": problems})
                print("\033[31m警告：外部锚点核对失败：" + "; ".join(problems) + "\033[0m", file=sys.stderr)
    files = [config_path]
    files += [base / cfg["trust"][k] for k in ("root_pin", "charter", "revocations", "issuer_key", "release")
              if k in cfg.get("trust", {})]
    framework = framework_fingerprint(code_dir, files, contents)
    try:
        setup = trust.load_setup(cfg["trust"], base) if "trust" in cfg else None
        if setup is not None:
            env = trust.loads_strict(contents[(base / cfg["trust"]["release"]).absolute()].decode("utf-8"))
            revocations = setup.revocations()
            body = trust.verify_release(setup.pin, setup.domain, env, revocations=revocations)
            trust.verify_charter(setup.pin, setup.domain, setup.charter_env, revocations, code_dir)
    except trust.TrustError as e:
        print(f"拒绝启动：信任链核对失败（{e.code}）：{e}", file=sys.stderr)
        return refused(e.code, str(e))
    release = None
    if setup is not None:
        checked = {"root": setup.pin.fingerprint, "domain": setup.domain, "slot": body["slot"],
                   "version": body["version"], "manifest_id": body["manifest_id"],
                   "revocations_version": revocations["body"]["version"] if revocations is not None else None}
        with _ledger(args, (cfg, base, raw)) as ledger:
            event = ledger.append("m2k.startup", "release.checked", {"task": contract.task_id, "release": checked})
            try:
                receipt = observer.witness(event, ledger.ledger_id)
            except obs.ObsError as e:
                code = "OBS_UNAVAILABLE"
                if isinstance(e, obs.ObsRefused):
                    code = ("ROOT_CHANGED" if str(e).startswith("root changed") else "RELEASE_ROLLBACK"
                            if str(e).startswith(("release rollback", "two manifests share", "revocations rollback"))
                            else "OBS_REFUSED")
                print(f"拒绝启动：{code}: {e}", file=sys.stderr)
                if code == "ROOT_CHANGED":
                    print("换 Root 的合法做法见 README 的“换 Root（合法做法）”一节。", file=sys.stderr)
                elif code == "RELEASE_ROLLBACK":
                    print("用 trust sign-release --slot <slot> --version <更高版本> 重签发行清单。", file=sys.stderr)
                ledger.append("m2k.startup", "startup.refused", {"task": contract.task_id, "code": code, "reason": str(e)})
                return 2
            ledger.append("m2k.startup", "obs.receipt", {"receipt": receipt})
        print(f"\033[1mRoot 指纹：{setup.pin.fingerprint}（{setup.domain}，OBS 已确认与首次见到的 Root 一致）\033[0m", file=sys.stderr)
        release = {"envelope": env, "config_sha256": trust.config_digest(raw), "checked_seq": event.seq}
    sandbox = sandboxes.choose(str(cfg.get("sandbox", {}).get("backend", "auto")))
    if isinstance(sandbox, sandboxes.Unavailable):
        print(f"拒绝启动：这台电脑没有可用的命令隔离环境（{sandbox.reason}）。\n"
              "两个选择：1) 在 GitHub 上运行（工作流 m2k-task）；"
              "2) 在 m2k.toml 写 [sandbox] backend = \"none\"，表示接受命令不隔离的风险（会记入账本）。", file=sys.stderr)
        return refused("SANDBOX_UNAVAILABLE", sandbox.reason)
    worker = build_model(models["worker"])
    reviewer = build_model({k: v for k, v in models["reviewer"].items() if k != "allow_same_model"})
    model = models["worker"].get("model", "").strip().casefold()
    same_model = bool((model and model == models["reviewer"].get("model", "").strip().casefold())
                      or worker.name == reviewer.name)
    if same_model and not allow_same_model:
        print(f"拒绝启动：Worker 和审查者是同一个模型（{worker.name}），不能自己审自己。"
              "请在 m2k.toml 给 [models.reviewer] 换一个模型。", file=sys.stderr)
        return refused("SAME_MODEL", f"Worker 和审查者是同一个模型（{worker.name}）")
    if same_model:
        print(f"\033[33m注意：Worker 和审查者是同一个模型（{worker.name}），按配置 allow_same_model "
              "以新会话独立审查（同源审查，已记入账本）。\033[0m", file=sys.stderr)
    problem = worker.probe() if getattr(worker, "external", False) else None   # before task submission
    if problem:
        print(f"拒绝启动：Worker 命令试启动失败（{worker.name}）：{problem}\n"
              "请先改好 [models.worker] 的 command（可用 m2k ping 单独检查），再运行。", file=sys.stderr)
        return refused("WORKER_PROBE", f"Worker 命令试启动失败（{worker.name}）：{problem}")
    if "trust" not in cfg:
        print("警告：未授权运行（没有信任链），已记入账本。", file=sys.stderr)
    if sandbox.name == "none":
        print("警告：命令不隔离运行（[sandbox] backend = \"none\"），已记入账本。", file=sys.stderr)
    ledger = cleanup.enter_context(_ledger(args, (cfg, base, raw)))
    if path and observer is None and ledger.ledger_id is not None:
        anchor.append(path, ledger, f"before {contract.task_id}")
    report = Kernel(ledger, worker, reviewer, sandbox=sandbox, trust=setup, framework=framework,
                    obs=observer, anchor_paths=(path, repo), release=release,
                    review_mode="same-model" if same_model else "cross-model", watch=watch).run(
                        contract, amends=args.amends, approval=approval)
    if path and (observer is None or ("anchor" not in cfg and ledger.last(contract.task_id, "obs.failed"))):
        anchor.append(path, ledger, f"{contract.task_id} {report.reported_verdict.value}")
    for e in ledger.events(contract.task_id, "authority.granted"):
        print(f"✓ 预授权放行（{e.payload['basis']['clause']}）：" + "；".join(f"{k} {a} → {b}" for k, (a, b) in e.payload["fields"].items()))
    print_report(report)
    if report.delivered_to is not None and observer is None:
        print("警告：本次交付没有回执（未配置 OBS），m2k check-delivery 会把它列为未经核验。", file=sys.stderr)
    if contract.deliver_to is not None and contract.deliver_to.is_dir():
        count = sum(bool(problems) for _, _, _, problems in delivery_entries(contract.deliver_to))
        if count:
            print(f"警告：交付目录里有 {count} 项未经核验的产出（m2k check-delivery <dir> 查看）。", file=sys.stderr)
    return 3 if report.awaiting else {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}[report.reported_verdict.value]


def cmd_recall(args) -> int:
    with _ledger(args) as ledger:
        refused = recall(ledger, args.task_id, args.reason)
    print(refused or f"recalled {args.task_id}: its next action is refused", file=sys.stderr if refused else sys.stdout)
    return 1 if refused else 0


def cmd_status(args) -> int:
    with _ledger(args) as ledger:
        try:
            print(json.dumps(status(ledger, args.task_id), ensure_ascii=False, indent=2))
        except KernelError as e:
            raise SystemExit(str(e))
    return 0


def cmd_log(args) -> int:
    with _ledger(args) as ledger:
        events = ledger.events(args.task_id)
    for e in events:
        payload = json.dumps(e.payload, ensure_ascii=False)
        if not args.full and len(payload) > 160:
            payload = payload[:157] + "..."
        op = f" [{e.op_id}]" if e.op_id else ""
        print(f"{e.seq:>5} {e.ts[11:23]} {e.kind:<18}{op} {payload}")
    return 0


def cmd_live(args) -> int:
    from .live import serve
    cfg, base, raw = _load_config(args.config)
    return serve(_ledger_path(args, (cfg, base, raw)), args.port, args.no_browser, args.task,
                 cfg if args.config else None, base)


def _anchor_path(args, override=None, config=None) -> Path | None:
    cfg, base, _ = config if config is not None else _load_config(args.config)
    section = cfg.get("anchor", {"file": cfg.get("anchor_file")})
    if "anchor" in cfg and (not isinstance(section, dict)
            or not isinstance(section.get("file"), str) or not section["file"]
            or ("repo_dir" in section and (not isinstance(section["repo_dir"], str) or not section["repo_dir"]))):
        raise ValueError("[anchor] must be a table with a non-empty string file (and repo_dir, if set)")
    return Path(override) if override else base / section["file"] if section.get("file") else None


def cmd_anchor(args) -> int:
    try:
        path = _anchor_path(args, args.out)
        if path is None:
            raise ValueError("set anchor_file in config or use --out FILE")
        with _ledger(args) as ledger:
            print(json.dumps(anchor.append(path, ledger, args.note), ensure_ascii=False))
        return 0
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1


def adjudication_inputs(cfg, base):
    """Load the same current trust and witness inputs for every release/checking path."""
    section, observer = cfg.get("trust", {}), cfg.get("obs", {})
    if "trust" in cfg and (not isinstance(section, dict) or not section.get("root_pin")):
        raise ValueError("config [trust] requires root_pin")
    if "obs" in cfg and (not isinstance(observer, dict) or not observer.get("log") or not observer.get("public_key_b64")):
        raise ValueError("config [obs] requires log and public_key_b64")
    return dict(root_pin=trust.load_pin(base / section["root_pin"]) if section else None,
                revocations=trust.read_json(base / section["revocations"]) if section.get("revocations") else None,
                obs_public_key=observer.get("public_key_b64"), obs_log=base / observer["log"] if observer else None)


def cmd_adjudicate(args) -> int:
    """Read-only: recompute a task's verdict from the ledger alone (T39). Exit 0 only on PASS."""
    from .adjudicate import adjudicate
    try:
        cfg, base, raw = _load_config(args.config)
        inputs = adjudication_inputs(cfg, base)
        with _ledger(args, (cfg, base, raw)) as ledger:
            adj = adjudicate(ledger, args.task_id, **inputs)
    except (OSError, ValueError, KeyError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps({"task_id": adj.task_id, "verdict": adj.verdict, "candidate": adj.candidate,
                      "reasons": adj.reasons}, ensure_ascii=False, indent=2))
    return 0 if adj.verdict == "PASS" else 1


def cmd_check_delivery(args) -> int:
    try:
        if args.standalone:
            if not args.public_key:
                raise ValueError("--standalone requires --public-key B64")
            results = check_delivery(args.directory, args.public_key)
        else:
            cfg, base, raw = _load_config(args.config)
            inputs = adjudication_inputs(cfg, base)
            if inputs["obs_public_key"] is None:
                raise ValueError("full delivery checks require [obs]; use --standalone --public-key B64")
            with _ledger(args, (cfg, base, raw)) as ledger:
                results = check_delivery(args.directory, ledger=ledger, **inputs)
        seen, passed = set(), 0
        for name, result in results.items():
            if id(result) in seen:
                continue
            seen.add(id(result))
            passed += not result["problems"]
            print(f"未经核验 {name}: {'; '.join(result['problems'])}" if result["problems"] else f"OK {name} ({result['files']} files)")
        if not results:
            print("OK empty delivery root")
        else:
            print(f"合计 {len(seen)} 份：通过 {passed} 份，未经核验 {len(seen) - passed} 份")
        return int(any(result["problems"] for result in results.values()))
    except (OSError, ValueError, KeyError) as e:
        print(str(e), file=sys.stderr)
        return 1


def cmd_check_ledger(args) -> int:
    cfg, base, raw = _load_config(args.config)
    try:
        path = _anchor_path(args, args.anchors, (cfg, base, raw))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    section = cfg.get("obs", {})
    log = base / section["log"] if section.get("log") else None
    with _ledger(args, (cfg, base, raw)) as ledger:
        ok, bad = ledger.verify_chain()
        print("ledger hash chain OK" if ok else f"ledger hash chain BROKEN at seq {bad}")
        problems = []
        if path:
            try:
                problems = anchor.check(path, ledger, section.get("public_key_b64"), None if args.remote else log)
                print("\n".join(problems) if problems else
                      f"anchors OK ({len(path.read_bytes().splitlines())} checked)")
            except (OSError, ValueError) as e:
                problems = [str(e)]
                print(problems[0])
        if args.remote:
            repo = Path(args.repo_dir) if args.repo_dir else base / cfg["anchor"]["repo_dir"] if cfg.get("anchor", {}).get("repo_dir") else None
            remote = (["set [anchor] repo_dir or use --repo-dir"] if repo is None else
                      anchor.remote_check(path, ledger, repo, section.get("public_key_b64"), log, args.this_ledger_only))
            problems.extend(remote)
            print("\n".join(remote) if remote else "remote anchors OK")
    return 0 if ok and not problems else 1


def cmd_report(args) -> int:
    from .report import write_report
    try:
        with _ledger(args) as ledger:
            path = write_report(ledger, args.task_id, args.out)
    except (KernelError, OSError, ValueError) as e:
        print(f"无法生成报告：{e}", file=sys.stderr)
        return 1
    print(path)
    return 0


def cmd_ping(args) -> int:
    """One tiny request per configured model role. Costs a few tokens; nothing is written to the ledger."""
    import time
    from .models import ModelError, extract_json
    cfg, _, _ = _load_config(args.config)
    roles = cfg.get("models", {})
    if not roles:
        raise SystemExit("config has no [models.*] sections")
    failed = 0
    for role, mcfg in roles.items():
        model = build_model({k: v for k, v in mcfg.items() if role != "reviewer" or k != "allow_same_model"})
        if getattr(model, "external", False):   # an agent CLI: start it once in an empty folder (T38)
            t = time.monotonic()
            problem = model.probe()
            failed += bool(problem)
            print(f"{'FAIL' if problem else 'OK  '} {role}: {model.name} (external agent) "
                  f"{problem or 'answered'} {time.monotonic() - t:.1f}s")
            continue
        t = time.monotonic()
        try:
            r = model.complete([{"role": "user", "content": 'Reply with exactly this JSON and nothing else: {"ok": true}'}])
            ok = extract_json(r.text).get("ok") is True
            print(f"{'OK  ' if ok else 'WARN'} {role}: model={r.model} tokens_in={r.input_tokens} "
                  f"tokens_out={r.output_tokens} {time.monotonic() - t:.1f}s"
                  + ("" if ok else f" (reply was not the expected JSON: {r.text[:120]!r})"))
        except (ModelError, ValueError) as e:
            failed += 1
            print(f"FAIL {role}: {e}")
            if hasattr(model, "list_models"):
                try:
                    print(f"     models available at this endpoint: {', '.join(model.list_models()) or '(none listed)'}")
                except ModelError as e2:
                    print(f"     (could not list models: {e2})")
    return 1 if failed else 0


def cmd_accept(args) -> int:
    from .accept import run_accept
    models = None
    if not args.offline:
        cfg, _, _ = _load_config(args.config)
        m = cfg.get("models", {})
        if "worker" not in m or "reviewer" not in m:
            raise SystemExit("config needs [models.worker] and [models.reviewer] (or use --offline)")
        models = (build_model(m["worker"]), build_model({k: v for k, v in m["reviewer"].items() if k != "allow_same_model"}))
    return run_accept(Path(args.dir), models)


def cmd_demo(args) -> int:
    from .demo import run_demo
    return run_demo(Path(args.dir))

def _trust_write(path, data, mode=0o666):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as f: f.write(data)

def cmd_obs(args) -> int:
    try:
        if args.obs_cmd == "serve": obs.serve(args.key, args.log, sys.stdin, sys.stdout)
        elif args.obs_cmd == "check":
            problems = obs.check_log(args.log, args.public_key)
            print("\n".join(problems) if problems else "OBS log OK")
            return int(bool(problems))
        else:
            out = Path(args.out).resolve()
            if out.exists() and (not out.is_dir() or any(out.iterdir())): raise FileExistsError(f"output exists: {out}")
            out.mkdir(parents=True, exist_ok=True)
            key, public = trust.generate_keypair()
            _trust_write(out / "obs-key.pem", key.private_bytes(trust.serialization.Encoding.PEM,
                         trust.serialization.PrivateFormat.PKCS8, trust.serialization.NoEncryption()), 0o600)
            print('[obs]\nkey = ' + json.dumps((out / "obs-key.pem").as_posix())
                  + '\nlog = ' + json.dumps((out / "obs.jsonl").as_posix()) + '\npublic_key_b64 = ' + json.dumps(public))
        return 0
    except (OSError, ValueError, TypeError) as e:
        print(str(e), file=sys.stderr); return 1

def cmd_trust(args) -> int:
    try:
        code_dir = Path(trust.__file__).resolve().parent
        if args.trust_cmd == "status":
            cfg, base, raw = _load_config(args.config)
            if "trust" not in cfg:
                print("no [trust] configured")
                return 1
            setup = trust.load_setup(cfg["trust"], base)
            print(f"\033[1mRoot 指纹：{setup.pin.fingerprint}（{setup.domain}）\033[0m")
            body, revocations = setup.charter_env["body"], setup.revocations()
            print("release: " + ("dev" if body.get("release") == "dev" else
                  "matches this code" if body.get("release") == trust.release_digest(code_dir) else "does not match"))
            try:
                trust.check_release(cfg["trust"], base, code_dir, raw)
            except trust.TrustError as e:
                state = {"RELEASE_CODE": "code changed", "RELEASE_CONFIG": "config changed"}.get(e.code, f"invalid ({e.code})")
                manifest = cfg["trust"].get("release")
                if manifest is None or (isinstance(manifest, str) and not (base / manifest).exists()):
                    state = "missing"
                print("release manifest: " + state)
                raise
            print("release manifest: ok")
            section = cfg.get("obs", {})
            try:
                base / section["log"]
                public = trust.public_key_b64(trust.load_private_key(base / section["key"]))
                state = "ok" if public == section["public_key_b64"] else "key mismatch"
            except (OSError, ValueError, KeyError, TypeError):
                state = "missing"
            print("obs: " + state + (" (log not yet created)" if state == "ok" and not (base / section["log"]).exists() else ""))
            if state != "ok":
                return 1
            remembered = obs.root_memory(base / section["log"], section["public_key_b64"]).get(setup.domain)
            root_changed = remembered is not None and remembered != setup.pin.fingerprint
            print("obs root: " + (f"changed (OBS remembers {remembered}, config uses {setup.pin.fingerprint})"
                  if root_changed else "ok" if remembered is not None else "not yet recorded"))
            trust.verify_charter(setup.pin, setup.domain, setup.charter_env, revocations, code_dir)
            matches = trust.public_key_b64(setup.issuer_key()) == body["issuer_public_key_b64"]
            print(json.dumps({"domain": setup.domain, "Root fingerprint": setup.pin.fingerprint, "charter id": body["charter_id"],
                  "issuer fingerprint": trust.fingerprint(trust._public(body["issuer_public_key_b64"])), "caps": body["caps"],
                  "revocation list version": revocations["body"]["version"] if revocations else None,
                  "issuer_key matches": matches}, ensure_ascii=False, indent=2))
            if not matches:
                raise trust.TrustError("ISSUER_KEY", "issuer_key does not match the charter")
            if root_changed:
                return 1
        elif args.trust_cmd == "init-dev":
            from .tools import TOOLS
            out = Path(args.out).resolve()
            if out.exists() and (not out.is_dir() or any(out.iterdir())):
                raise FileExistsError(f"output exists: {out}")
            out.mkdir(parents=True, exist_ok=True)
            with redirect_stdout(io.StringIO()):
                for role in ("root", "issuer"):
                    if main(["trust", "keygen", "--role", role, "--out", str(out / role)]): return 1
                _trust_write(out / "caps.toml", (f'tools = {json.dumps(sorted(TOOLS))}\n'
                             'max_rounds = 100\nmax_model_calls = 120\nmax_tokens = 10000000\n'
                             'max_commands = 100\nnetwork = false\n').encode("utf-8"))
                common = ["--root-key", str(out / "root.pem"), "--domain", "TEST_ONLY"]
                if main(["trust", "sign-charter", *common, "--issuer-pin", str(out / "issuer-pin.json"),
                         "--caps", str(out / "caps.toml"), "--release", "dev", "--out", str(out / "charter.json")]): return 1
                if main(["trust", "sign-revocations", *common, "--version", "0",
                         "--out", str(out / "revocations.json")]): return 1
            print("Root fingerprint: " + trust.load_pin(out / "root-pin.json").fingerprint)
            print('[trust]\ndomain = "TEST_ONLY"')
            for field, filename in (("root_pin", "root-pin.json"), ("charter", "charter.json"),
                                    ("issuer_key", "issuer.pem"), ("revocations", "revocations.json"), ("release", "release.json")):
                print(f"{field} = {json.dumps((out / filename).as_posix(), ensure_ascii=False)}")
            if main(["obs", "keygen", "--out", str(out / "obs")]):
                return 1
            print(f"# 把表加入 m2k.toml 后运行：m2k trust sign-release --root-key {json.dumps((out / 'root.pem').as_posix(), ensure_ascii=False)} "
                  f"--domain TEST_ONLY --code dev --slot dev --version 1 --out {json.dumps((out / 'release.json').as_posix(), ensure_ascii=False)}")
            print("TEST_ONLY：只用于开发和测试。正式运行需要用真 Root 离线签发的授权书。")
        elif args.trust_cmd == "keygen":
            if args.role == "root" and args.domain == "PRODUCTION":
                print("正式 Root 只能在离线环境用自己的工具生成；本命令只生成 TEST_ONLY 的 Root", file=sys.stderr)
                return 2
            pem, pin = Path(args.out + ".pem"), Path(args.out + "-pin.json")
            for path in (pem, pin):
                if path.exists() or path.is_symlink():
                    raise FileExistsError(f"output exists: {path}")
            key, public = trust.generate_keypair()
            fp = trust.fingerprint(key.public_key())
            _trust_write(pem, key.private_bytes(trust.serialization.Encoding.PEM,
                         trust.serialization.PrivateFormat.PKCS8, trust.serialization.NoEncryption()), 0o600)
            _trust_write(pin, trust.canonical({"schema": "m2k.pin/1", "trust_domain": args.domain,
                         "public_key_b64": public, "fingerprint": fp}))
            print(f"{pin}: {fp}")
        else:
            if args.trust_cmd == "sign-charter" and args.domain == "PRODUCTION" and args.release == "dev":
                print("PRODUCTION cannot use a dev release", file=sys.stderr)
                return 2
            if args.trust_cmd == "sign-release" and args.domain == "PRODUCTION" and args.code == "dev":
                print("PRODUCTION cannot use a dev release", file=sys.stderr)
                return 2
            key = trust.load_private_key(args.root_key)
            body = {"schema": trust.CHARTER if args.trust_cmd == "sign-charter" else trust.REVOCATIONS,
                    "trust_domain": args.domain, "issued_at": int(time.time())}
            if args.trust_cmd == "sign-charter":
                issuer = trust.load_pin(args.issuer_pin)
                if issuer.trust_domain != args.domain:
                    raise trust.TrustError("DOMAIN", "issuer pin must match the charter domain")
                caps = tomllib.loads(Path(args.caps).read_text(encoding="utf-8"))
                trust._grant_fits([], {k: 0 for k in trust.BUDGET_KEYS}, False, caps)
                release = args.release or ("dev" if args.domain == "TEST_ONLY" else "current")
                body.update(charter_id=args.charter_id or trust.secrets.token_hex(16), caps=caps,
                            issuer_public_key_b64=trust._b64(trust._raw(issuer.public_key)),
                            release=trust.release_digest(code_dir) if release == "current" else release)
            elif args.trust_cmd == "sign-release":
                cfg, base, raw = _load_config(args.config)
                version = args.version
                if version is None:
                    try:
                        current = trust.read_json(base / cfg["trust"]["release"])
                        pin = trust.Pin(key.public_key(), args.domain, trust.fingerprint(key.public_key()))
                        previous = trust.verify_release(pin, args.domain, current)
                        if previous["slot"] != args.slot:
                            raise ValueError("different slot")
                        version = previous["version"] + 1
                    except (KeyError, OSError, ValueError, TypeError) as e:
                        raise trust.TrustError("RELEASE_VERSION", "current manifest must verify for this Root and slot; specify --version") from e
                body.update(schema=trust.RELEASE, manifest_id=trust.secrets.token_hex(16), slot=args.slot, version=version,
                            code=args.code or trust.release_digest(code_dir),
                            config=trust.config_digest(raw))
            else:
                body.update(version=args.version, revoked_charters=args.charter, revoked_credentials=args.credential, revoked_releases=args.release)
            envelope = trust.sign(body, key)
            trust.open_envelope(envelope, key.public_key())
            if args.trust_cmd == "sign-release":
                trust.verify_release(trust.Pin(key.public_key(), args.domain, trust.fingerprint(key.public_key())), args.domain, envelope)
            _trust_write(args.out, trust.canonical(envelope))
            if args.trust_cmd == "sign-release":
                print(f"slot {args.slot} version {version}")
            print(args.out)
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        print(f"{e.strerror}: {e.filename}" if isinstance(e, OSError) and e.filename else
              str(e) if isinstance(e, (OSError, trust.TrustError)) else "FORMAT: invalid trust input", file=sys.stderr)
        return 1

def cmd_isolation(args) -> int:
    if args.isolation_cmd == "check":
        from .isolation import check
        code, report = check(Path(args.config or DEFAULT_CONFIG))
    else:
        from .isolation_install import execute
        code, report = execute(Path(args.config or DEFAULT_CONFIG), args.isolation_cmd)
    print(report)
    return code


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows GBK consoles: never crash on output
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap =argparse.ArgumentParser(prog="m2k", description="M2 governance kernel")
    ap.add_argument("--config", help=f"config file (default ./{DEFAULT_CONFIG} if present)")
    ap.add_argument("--ledger", help=f"ledger path (default from config, else {_default_ledger()})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run a task contract through the full chain")
    p.add_argument("--untrusted", action="store_true", help="只这一次无信任链运行（未授权，会记入账本）")
    p.add_argument("--amends", help="start a new contract version from a finished task's last candidate")
    p.add_argument("task", help="task contract .toml")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("answer", help="所有者在交互终端批准请求并开新版本")
    p.add_argument("amends", help="待修订任务 ID")
    p.add_argument("task", help="新契约 .toml")
    p.add_argument("--text", help="所有者回答（写入新契约约束）")
    p.add_argument("--untrusted", action="store_true", help="只这一次无信任链运行")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("recall", help="recall a running subtask: its authority is withdrawn, its next action refused")
    p.add_argument("task_id")
    p.add_argument("--reason", default="recalled by the owner")
    p.set_defaults(fn=cmd_recall)
    p = sub.add_parser("status", help="summarize a task from the ledger")
    p.add_argument("task_id")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("log", help="print a task's ledger events")
    p.add_argument("task_id")
    p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_log)
    p = sub.add_parser("live", help="只读观看或回放真实账本")
    p.add_argument("--task")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_live)
    p = sub.add_parser("report", help="生成任务账本的 HTML 时间线")
    p.add_argument("task_id")
    p.add_argument("--out", help="输出文件（默认 .m2k/reports/<task_id>.html）")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("adjudicate", help="recompute a task's verdict from the ledger alone (read-only)")
    p.add_argument("task_id")
    p.set_defaults(fn=cmd_adjudicate)
    p = sub.add_parser("check-delivery", help="verify every entry of a delivery root against OBS receipts")
    p.add_argument("directory")
    p.add_argument("--standalone", action="store_true")
    p.add_argument("--public-key")
    p.set_defaults(fn=cmd_check_delivery)
    p = sub.add_parser("check-ledger", help="verify the ledger hash chain")
    p.add_argument("--anchors", help="anchor file (default from config)")
    p.add_argument("--remote", action="store_true", help="check published anchors and GitHub branch activity")
    p.add_argument("--repo-dir", help="dedicated ledger-anchors clone (default from [anchor])")
    p.add_argument("--this-ledger-only", action="store_true", help="skip cross-ledger OBS spine checks for disposable CI")
    p.set_defaults(fn=cmd_check_ledger)
    p = sub.add_parser("anchor", help="append the ledger head to an anchor file")
    p.add_argument("--out", help="anchor file (default from config)")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_anchor)
    p = sub.add_parser("accept", help="scenario acceptance table (offline scenarios + real-model scenarios)")
    p.add_argument("--offline", action="store_true", help="only the free, scripted scenarios")
    p.add_argument("--dir", default=".m2k")
    p.set_defaults(fn=cmd_accept)
    p = sub.add_parser("ping",help="check that each configured model answers (costs a few tokens)")
    p.set_defaults(fn=cmd_ping)
    p = sub.add_parser("demo",help="offline demo with scripted models (no API needed)")
    p.add_argument("--dir", default="m2k-demo")
    p.set_defaults(fn=cmd_demo)
    commands = sub.add_parser("isolation", help="检查和安装 Windows 账号隔离").add_subparsers(dest="isolation_cmd", required=True)
    commands.add_parser("check", help="报告有效读写和执行权限").set_defaults(fn=cmd_isolation)
    for name in ("install", "grant", "revoke", "uninstall"):
        commands.add_parser(name, help="需要管理员终端与交互确认").set_defaults(fn=cmd_isolation)
    commands = sub.add_parser("obs", help="manage the independent OBS witness").add_subparsers(dest="obs_cmd", required=True)
    for name, options in (("keygen", ("out",)), ("serve", ("key", "log")), ("check", ("log",))):
        p = commands.add_parser(name); p.set_defaults(fn=cmd_obs)
        for option in options: p.add_argument("--" + option, required=True)
        if name == "check": p.add_argument("--public-key")
    commands = sub.add_parser("trust", help="manage the Root trust chain").add_subparsers(dest="trust_cmd", required=True)
    for name, required in (("keygen", ("role",)), ("sign-charter", ("root-key", "issuer-pin", "caps")),
                           ("sign-revocations", ("root-key", "version")), ("sign-release", ("root-key", "slot")), ("status", ()), ("init-dev", ())):
        p = commands.add_parser(name)
        p.set_defaults(fn=cmd_trust)
        if name == "status": continue
        p.add_argument("--out", required=True)
        if name == "init-dev": continue
        p.add_argument("--domain", choices=trust.DOMAINS, **({"default": "TEST_ONLY"} if name == "keygen" else {"required": True}))
        for option in required:
            p.add_argument("--" + option, required=True, **({"choices": ("root", "issuer")} if option == "role" else
                           {"type": int} if option == "version" else {}))
        if name == "sign-charter":
            p.add_argument("--release", choices=("dev", "current"))
            p.add_argument("--charter-id")
        if name == "sign-release":
            p.add_argument("--code", choices=("dev",))
            p.add_argument("--version", type=int)
        if name == "sign-revocations":
            for option in ("charter", "credential", "release"):
                p.add_argument("--" + option, nargs="+", action="extend", default=[])
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
