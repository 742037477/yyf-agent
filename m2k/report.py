"""Read-only, self-contained HTML timeline of a task's ledger."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .kernel import status
from .ledger import Event, Ledger

CSS = """
body { margin: 2rem auto; max-width: 1200px; padding: 0 1rem;
       font-family: system-ui, sans-serif; color: #17212b; background: #f8fafc; }
header, table { background: white; border: 1px solid #d8dee6; border-radius: 8px; }
header { padding: 1rem; margin-bottom: 1rem; }
.untrusted { color: #991b1b; background: #fff1f2; border: 1px solid #991b1b; padding: 1rem; margin-bottom: 1rem; }
h1 { font-size: 1.5rem; overflow-wrap: anywhere; }
dl { display: grid; grid-template-columns: 8rem 1fr; gap: .6rem; }
dt { font-weight: bold; } dd { margin: 0; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: .8rem; border-bottom: 1px solid #d8dee6; text-align: left; vertical-align: top; }
th { background: #edf2f7; } td:first-child { width: 14rem; }
.pass { color: #166534; } .fail { color: #991b1b; }
.unknown { color: #92400e; } .none { color: #59636f; }
tr.fail { background: #fff1f2; } tr.unknown { background: #fffbeb; }
.summary, pre { white-space: pre-wrap; overflow-wrap: anywhere; }
pre { background: #f1f5f9; padding: .8rem; max-height: 25rem; overflow: auto; color: #17212b; }
details { margin-top: .5rem; } summary { cursor: pointer; }
@media (max-width: 700px) { body { margin: 1rem auto; } th, td { padding: .4rem; }
                          dl { grid-template-columns: 6rem 1fr; } }
"""


def _text(value) -> str:
    return escape("未记录" if value is None else str(value))


def _summary(event: Event) -> str:
    p, kind = event.payload, event.kind
    if kind == "task.submitted":
        return "提交任务：" + str(p.get("goal", ""))[:300]
    if kind in ("proposal", "effect.intent"):
        args = p.get("args", {})
        target = args.get("path", args.get("command", ""))
        action = f"{p.get('tool', '')} {target}".strip()
        if kind == "proposal":
            return f"第 {p.get('round', '?')} 轮：{action[:200]} — {str(p.get('thought', ''))[:200]}"
        return "准备执行：" + action[:300]
    if kind == "gate.denied":
        return f"拒绝执行：{p.get('gate', '')} — {p.get('reason', '')}"
    if kind == "effect.result":
        return f"{p.get('status', 'UNKNOWN')}：{str(p.get('output', ''))[:300]}"
    if kind in ("verdict", "audit.result", "test.result"):
        reasons = p.get("reasons", [p.get("reason", "")])
        return f"判定 {p.get('verdict', 'UNKNOWN')}：" + "; ".join(map(str, reasons))[:300]
    if kind == "worker.stopped":
        return f"执行者 {p.get('status', '')}：{p.get('reason', '')} — {str(p.get('summary', ''))[:200]}"
    if kind == "model.called":
        return (f"模型调用：{p.get('role', '')} / {p.get('model', '')}；"
                f"输入令牌：{p.get('input_tokens') if p.get('input_tokens') is not None else '未知'}；"
                f"输出令牌：{p.get('output_tokens') if p.get('output_tokens') is not None else '未知'}")
    if kind in ("proposal.invalid", "model.error"):
        return "错误：" + str(p.get("error", ""))[:300]
    if kind == "candidate.frozen":
        return f"冻结候选：{len(p.get('files', {}))} 个文件；摘要 {p.get('digest', '')}"
    if kind == "delivery.intent":
        return "准备交付：" + str(p.get("target", ""))
    if kind == "delivery.result":
        checked = "通过" if p.get("post_verified") else "未通过"
        return f"交付：{p.get('target', '')}；交付后核验：{checked}"
    if kind == "delivery.refused":
        return "拒绝交付：" + str(p.get("reason", ""))
    return json.dumps(p, ensure_ascii=False)[:300]


def _tone(event: Event) -> str:
    if event.kind in ("gate.denied", "proposal.invalid", "model.error", "delivery.refused"):
        return "fail"
    if event.kind == "effect.result":
        return {"FAILED": "fail", "UNKNOWN": "unknown"}.get(event.payload.get("status"), "")
    verdict = event.payload.get("verdict")
    if verdict is not None and verdict != "PASS":
        return "fail" if verdict == "FAIL" else "unknown"
    return ""


def write_report(ledger: Ledger, task_id: str, out: str | Path | None = None) -> Path:
    state = status(ledger, task_id)  # Raises KernelError before creating a file for an unknown task.
    path = Path(out) if out is not None else Path(".m2k/reports") / f"{task_id}.html"
    if out is None and not path.resolve().is_relative_to(Path(".m2k/reports").resolve()):
        raise ValueError("任务编号超出默认报告目录，请用 --out 指定文件")
    if path.resolve() == ledger.path.resolve() or (path.exists() and path.samefile(ledger.path)):
        raise ValueError("报告输出不能覆盖账本")
    events = ledger.events(task_id)
    ok, bad_seq = ledger.verify_chain()
    verdict = state["verdict"]
    tone, label = {"PASS": ("pass", "通过（PASS）"), "FAIL": ("fail", "失败（FAIL）"),
                   "UNKNOWN": ("unknown", "未知（UNKNOWN）"), None: ("none", "无判定")}.get(
                       verdict, ("unknown", "未知（UNKNOWN）"))
    usage = state["usage"]
    usage_text = "；".join(f"{label}：{usage[key]}" for key, label in (
        ("rounds", "轮次"), ("model_calls", "模型调用"), ("tokens", "已知令牌"),
        ("tokens_unknown_calls", "用量未知调用"), ("commands", "命令")))
    chain = "通过" if ok else f"断裂（事件序号 {bad_seq}）"
    authorization = next(e.payload.get("trust") for e in reversed(events) if e.kind == "task.submitted")
    trust_html = ('<div class="untrusted">未授权运行：这个任务没有信任链（Root → 授权书 → 凭证）</div>'
                  if authorization is None else '<p>信任链：' + '；'.join(
                      f'{label}：{_text(authorization.get(key))}' for key, label in (
                          ("domain", "域"), ("root_fingerprint", "Root 指纹"),
                          ("charter_id", "授权书 ID"), ("credential_id", "凭证 ID"))) + '</p>')
    parts = [f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width, initial-scale=1">'
             f'<title>任务 {_text(task_id)} 时间线</title><style>{CSS}</style></head><body>',
             f'{trust_html}<header><h1>任务 {_text(task_id)}</h1><dl>'
             f'<dt>目标</dt><dd>{_text(state["goal"])}</dd>'
             f'<dt>最终判定</dt><dd class="{tone}">{label}</dd>'
             f'<dt>执行者状态</dt><dd>{_text(state.get("worker", {}).get("status"))}</dd>'
             f'<dt>审查模式</dt><dd>{_text(state["review"])}</dd>'
             f'<dt>用量</dt><dd>{_text(usage_text)}</dd>'
             f'<dt>账本哈希链</dt><dd class="{"pass" if ok else "fail"}">{_text(chain)}</dd>'
             '</dl></header><table><thead><tr><th>时间（UTC）</th><th>事件类型</th>'
             '<th>摘要与详情</th></tr></thead><tbody>']
    for event in events:
        raw = json.dumps(event.payload, ensure_ascii=False, indent=2)
        parts.append(f'<tr class="{_tone(event)}"><td>{_text(event.ts)}</td><td>{_text(event.kind)}</td>'
                     f'<td><div class="summary">{_text(_summary(event))}</div><details>'
                     f'<summary>原始载荷（JSON）</summary><pre>{_text(raw)}</pre></details></td></tr>')
    parts.append("</tbody></table></body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
