"""Local, read-only ledger observer; animation mapping contains no decisions."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import webbrowser
from contextlib import closing, nullcontext
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .adjudicate import adjudicate
from .ledger import Event, Ledger


def select_events(events, task):
    ids = {task}
    while True:
        children = {e.task_id for e in events if e.kind == "task.submitted" and e.payload.get("parent") in ids}
        if children <= ids:
            break
        ids.update(children)
    selected = [e for e in events if e.task_id in ids or
                (e.task_id == "m2k.startup" and e.payload.get("task") in ids)]
    seqs = {e.seq for e in selected}
    return [e for e in events if e.seq in seqs or (e.kind == "obs.receipt" and
            e.payload.get("receipt", {}).get("body", {}).get("ledger_seq") in seqs)]


def animal(model):
    name = str(model).casefold().rsplit(":", 1)[-1]   # "openai_compat:glm-5.3": the adapter kind is not the model
    return next((a for keys, a in ((('deepseek', 'dsh'), 'whale'), (('claude',), 'crab'), (('glm',), 'fox'),
                (('codex', 'gpt', 'openai'), 'dragon'))
                 if any(k in name for k in keys)), "robot")


def refusal_lamp(code, released=False):
    if code in ("ROOT_CHANGED", "RELEASE_ROLLBACK", "OBS_REFUSED"):
        return 8
    if code == "OBS_UNAVAILABLE":
        return 8 if released else 4
    for prefix, lamp in (("CONFIG", 1), ("RELEASE", 3), ("OBS_REQUIRED", 4), ("OBS_CONFIG", 4),
                         ("ANCHOR_", 5), ("FRAMEWORK", 6), ("CHARTER", 7), ("EXCEEDS_CHARTER", 7),
                         ("SANDBOX_UNAVAILABLE", 9), ("SAME_MODEL", 10), ("WORKER_PROBE", 11)):
        if code.startswith(prefix):
            return lamp
    return 2


def station(kind, payload, child=False):
    if kind == "obs.failed":
        return station(payload.get("kind", ""), {}, child)
    where = {"task.submitted": "contract", "credential.issued": "cred", "candidate.frozen": "frozen",
             "test.result": "test", "audit.result": "review", "verdict": "verdict",
             "gate.adjudication": "adjud", "delivery.result": "gate", "delivery.refused": "gate"}.get(kind)
    if kind == "model.called":
        where = "review" if payload.get("role") == "reviewer" else "worker"
    if kind.startswith(("effect.", "plan.", "budget.", "subtask.", "repair.")) or kind in (
            "gate.denied", "worker.stopped", "model.error", "workspace.changed", "outside.changed"):
        where = "worker"
    if kind in ("anchor.checkpoint", "anchor.published", "anchor.failed"):
        where = "anchor"
    return "worker" if child and where in ("test", "review", "verdict") else where


def animation_steps(events):
    """Recompute context before pagination, including receipts appended after their events."""
    witnessed = {e.payload.get("receipt", {}).get("body", {}).get("ledger_seq")
                 for e in events if e.kind == "obs.receipt"}
    submitted = {e.task_id: e.payload for e in events if e.kind == "task.submitted"}
    lamps, released, totals, tree, children, steps = {}, set(), {}, {}, {}, []
    for e in events:
        p, kind, tid = e.payload, e.kind, e.task_id
        if kind == "obs.receipt":
            continue
        meta = submitted.get(tid, {})
        s = {"seq": e.seq, "ts": e.ts, "task": p.get("task", tid) if tid == "m2k.startup" else tid,
             "kind": kind, "station": station(kind, p, bool(meta.get("parent"))),
             "state": "ok", "obs": e.seq in witnessed, "caption": kind}
        detail, changed = {}, {}
        if kind == "startup.refused":
            lamp = refusal_lamp(str(p.get("code", "")), p.get("task") in released)
            changed = {str(i): "ok" for i in range(1, lamp)} | {str(lamp): "bad"}
            s.update(state="bad", caption="被拦下：" + str(p.get("reason", p.get("code", "")))[:160])
        elif kind == "anchor.warning":
            changed, s["state"] = {"5": "warn"}, "warn"
        elif kind == "release.checked":
            released.add(p.get("task"))
            changed = {"8": "ok" if s["obs"] else "unknown"}
            s["caption"] = "发行核对：Root 记忆已旁证" if s["obs"] else "发行核对：等待 OBS 旁证"
        elif kind == "task.submitted":
            changed = {str(i): "ok" for i in range(1, 12) if str(i) not in lamps}
            for condition, numbers, value in ((p.get("trust") is None, (2, 3, 7, 8), "skip"),
                    (p.get("obs") is None, (4, 8), "skip"), (p.get("sandbox") == "none", (9,), "warn"),
                    (p.get("review") == "same-model", (10,), "warn")):
                if condition:
                    changed.update({str(i): value for i in numbers})
            detail["review"], s["caption"] = p.get("review", "cross-model"), "提交任务契约"
        if changed:
            lamps.update(changed)
            s["lamps"] = changed
        for sub in p.get("subtasks", []) if kind in ("task.submitted", "plan.accepted") else []:
            tree[(tid, sub["id"])] = "pending"
        if kind in ("subtask.started", "subtask.finished"):
            key = (tid, p.get("id", p.get("task_id")))
            children[p.get("task_id")] = key
            tree[key] = "run" if kind.endswith("started") else p.get("verdict", "UNKNOWN")
        if kind == "test.result" and tid in children:
            tree[children[tid]] = p.get("verdict", "UNKNOWN")
        role, action = "worker", None
        if kind == "credential.issued":
            action, s["caption"] = "enter", "签发任务凭证"
        elif kind == "model.called":
            role = p.get("role", "worker")
            action = "enter" if role == "reviewer" else "think"
            detail.update({k: p[k] for k in ("model", "round") if k in p})
            if role in ("worker", "planner"):
                usage = (p.get("input_tokens"), p.get("output_tokens"))
                totals[tid] = (totals.get(tid, 0) + sum(usage) if totals.get(tid, 0) is not None
                               and all(isinstance(n, int) for n in usage) else None)
            s["caption"] = "独立审查者入场" if role == "reviewer" else "执行者思考"
            s["state"] = "active"
        elif kind == "effect.intent":
            tool, args = p.get("tool", ""), p.get("args", {})
            action = {"list_files": "files", "read_file": "files", "write_file": "edit", "edit_file": "edit",
                      "run_command": "cmd", "external_agent": "busy"}.get(tool, "busy")
            detail["call"] = (str(tool) + " " + str(args.get("path", args.get("command", ""))))[:180].strip()
            s.update(state="active", caption="提议 " + detail["call"] + " → Gate 放行")
            if tool == "external_agent":
                s["caption"] = "外部 Agent 工作中：框架看不到其内部过程"
        elif kind == "workspace.changed":
            counts = {k: len(p.get(k, [])) + p.get("more", {}).get(k, 0) for k in ("added", "changed", "removed")}
            action = "edit" if counts["added"] or counts["changed"] else "files"
            s["caption"] = f"框架观测到工作区变化：新增 {counts['added']}、修改 {counts['changed']}、删除 {counts['removed']}"
        elif kind == "outside.changed":
            count = sum(len(p.get(k, [])) + p.get("more", {}).get(k, 0) for k in ("added", "changed", "removed"))
            s.update(state="unknown" if "unverified" in p else "bad", caption="工作区外的监视路径" +
                     ("无法核验" if "unverified" in p else f"被改动：{count} 处"))
        elif kind == "effect.result":
            action, s["caption"] = "home", "执行结果：" + str(p.get("status", "UNKNOWN"))
            s["state"] = {"COMPLETED": "ok", "FAILED": "bad", "DENIED": "bad"}.get(p.get("status"), "unknown")
        elif kind == "gate.denied":
            action = "denied"
            s.update(state="bad", caption="被拦下：" + str(p.get("reason", ""))[:160])
        elif kind == "worker.stopped":
            action = "done" if p.get("status") == "FINISHED" else "denied"
            s.update(state="ok" if action == "done" else "bad", caption="执行者停止：" + str(p.get("status", "UNKNOWN")))
        elif kind in ("test.result", "audit.result", "verdict", "gate.adjudication"):
            s["state"] = {"PASS": "ok", "FAIL": "bad"}.get(p.get("verdict"), "unknown")
            s["caption"] = {"test.result": f"任务方的验收：{p.get('verdict', 'UNKNOWN')} · 退出码 {p.get('exit_code')}",
                            "audit.result": "独立审查：" + str(p.get("decision", "UNKNOWN")),
                            "verdict": "内核判定 " + str(p.get("verdict", "UNKNOWN")),
                            "gate.adjudication": "只读裁决 " + str(p.get("verdict", "UNKNOWN"))}[kind][:200]
            if kind == "audit.result":
                role, action = "reviewer", "verdict"
        elif kind == "candidate.frozen":
            s["caption"] = "冻结候选产物"
        elif kind in ("obs.failed", "anchor.failed", "delivery.refused", "model.error", "budget.refused", "plan.rejected"):
            s.update(state="bad", caption="被拦下：" + str(p.get("reason", kind))[:160])
        elif s["station"] is not None:
            s["caption"] = {"task.submitted": "提交任务", "credential.issued": "签发任务凭证", "anchor.checkpoint": "锚点检查点", "anchor.published": "锚点已发布",
                            "delivery.result": "Gate 已交付", "subtask.started": "子任务开始",
                            "subtask.finished": "子任务结束", "plan.accepted": "方案已接受",
                            "plan.proposed": "提议任务方案", "budget.reserved": "预留预算",
                            "budget.released": "释放预算", "repair.started": "开始修复"}.get(kind, kind)
        elif kind not in ("startup.refused", "anchor.warning", "release.checked"):
            s["state"] = "unknown"
        if action:
            s["actor"] = {"role": role, "animal": animal(meta.get("reviewer_model" if role == "reviewer" else "worker_model")), "do": action}
        if tid in totals:
            detail["tokens"] = totals[tid]
        if tree:
            detail["tree"] = [[f"{parent}.{sid}", value] for (parent, sid), value in tree.items()]
        if detail:
            s["detail"] = detail
        steps.append(s)
    return steps


def trim(value, depth=0):
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: ("<signed>" if k in ("envelope", "receipt", "signature_b64") else
                    f"<{len(v) if isinstance(v, str) else len(json.dumps(v))} chars>")
                if k in ("output", "prompt", "envelope", "receipt", "signature_b64") else trim(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [trim(v, depth + 1) for v in value]
    return value[:300] + "…" if isinstance(value, str) and len(value) > 300 else value


def read_events(db):
    return [Event(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6], r[7]) for r in db.execute(
        "SELECT seq,task_id,kind,op_id,payload,ts,prev_hash,hash FROM events ORDER BY seq")]


def task_list(events):
    children = {e.task_id for e in events if e.kind == "task.submitted" and e.payload.get("parent")}
    ids = dict.fromkeys(e.payload.get("task") if e.task_id == "m2k.startup" else e.task_id for e in events)
    tasks = []
    for tid in ids:
        if not tid or tid in children:
            continue
        own = select_events(events, tid)
        if not own:
            continue
        row = dict(task=tid, first_seq=own[0].seq, last_seq=own[-1].seq, first_ts=own[0].ts,
                   last_ts=own[-1].ts, last_kind=own[-1].kind,
                   verdict=next((e.payload.get("verdict") for e in reversed(own) if e.task_id == tid and e.kind == "verdict"), None))
        if all(e.task_id == "m2k.startup" and e.kind == "startup.refused" for e in own):
            row["refused"] = own[-1].payload.get("code")
        tasks.append(row)
    return sorted(tasks, key=lambda t: t["last_seq"], reverse=True)


def make_server(path, port=8765, task=None, cfg=None, base=None):
    path = Path(path).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def __getattr__(self, name):
            if name.startswith("do_"):
                return lambda: self.send(405, {"error": "GET only"})
            raise AttributeError(name)

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                if url.path == "/":
                    return self.send(200, Path(__file__).with_name("live.html").read_bytes(), "text/html; charset=utf-8")
                if url.path not in ("/api/tasks", "/api/steps", "/api/events", "/api/adjudicate"):
                    return self.send(404, {"error": "unknown endpoint"})
                query = parse_qs(url.query, keep_blank_values=True)
                allowed = () if url.path == "/api/tasks" else ("task",) if url.path == "/api/adjudicate" else ("task", "after", "limit")
                if any(k not in allowed or len(v) != 1 for k, v in query.items()):
                    raise ValueError("invalid or duplicate parameters")
                tid = query.get("task", [task])[0]
                if url.path != "/api/tasks" and (not tid or not tid.strip()):
                    raise ValueError("task is required")
                after, limit = int(query.get("after", [0])[0]), int(query.get("limit", [500])[0])
                if after < 0 or not 1 <= limit <= 500:
                    raise ValueError("after >= 0; 1 <= limit <= 500 required")
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) if path.exists() else nullcontext(None) as db:
                    state = "ok" if db is not None else "missing"
                    if url.path == "/api/adjudicate":
                        result = self.adjudication(db, tid) if db else dict(verdict="UNKNOWN", reasons=["ledger missing"], flagged_seqs=[])
                    else:
                        events = read_events(db) if db else []
                        if url.path == "/api/tasks":
                            result = {"tasks": task_list(events)}
                        else:
                            selected = select_events(events, tid)
                            done = any(e.kind in ("startup.refused", "delivery.result", "delivery.refused") or
                                       (e.task_id == tid and e.kind == "gate.adjudication" and not any(
                                           s.task_id == tid and s.kind == "task.submitted" and s.payload.get("parent") for s in selected)) for e in selected)
                            values = animation_steps(selected) if url.path == "/api/steps" else [asdict(e) | {"payload": trim(e.payload)} for e in selected]
                            page = [v for v in values if v["seq"] > after][:limit]
                            result = {"steps" if url.path == "/api/steps" else "events": page,
                                      "last_seq": page[-1]["seq"] if page else after, "done": done}
                self.send(200, {"ledger": state, **result})
            except (ValueError, TypeError, KeyError) as e:
                self.send(400, {"error": str(e)})
            except (OSError, sqlite3.Error) as e:
                self.send(500, {"error": str(e)})

        def adjudication(self, db, tid):
            from .cli import adjudication_inputs
            with tempfile.TemporaryDirectory(prefix="m2k-live-") as directory:
                copy = Path(directory) / "ledger.sqlite"
                with closing(sqlite3.connect(copy)) as target:
                    db.backup(target)
                with Ledger(copy) as ledger:
                    adj = adjudicate(ledger, tid, **(adjudication_inputs(cfg, base) if cfg is not None else {}))
            flagged = sorted({int(n) for reason in adj.reasons if "obs" in reason.casefold() or "receipt" in reason.casefold()
                              for n in re.findall(r"seq (\d+)", reason)})
            return dict(verdict=adj.verdict, reasons=adj.reasons, flagged_seqs=flagged)
    class Server(ThreadingHTTPServer):
        allow_reuse_address = False
    return Server(("127.0.0.1", port), Handler)


def serve(path, port=8765, no_browser=False, task=None, cfg=None, base=None):
    try:
        server = make_server(path, port, task, cfg, base)
    except (OSError, ValueError, OverflowError) as e:
        print(f"m2k live 无法启动：{e}", file=sys.stderr)
        return 1
    with server:
        url = f"http://127.0.0.1:{server.server_port}/" + ("?" + urlencode({"task": task}) if task else "")
        print(url, flush=True)
        if not no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
