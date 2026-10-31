"""Read-only checks to run before inserting the offline Root signing USB."""
from __future__ import annotations

import argparse
import csv
import io
import os
import platform
import queue
import re
import socket
import subprocess
import threading
from pathlib import Path


AGENTS = ("codex", "claude", "chatgpt", "cursor", "copilot", "windsurf", "ollama",
          "lmstudio", "deepseek", "zcode", "cc-switch", "node", "python")
RUN_KEYS = (r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run")


def command(args: list[str], *, registry: bool = False) -> str:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ValueError(f"{args[0]}: {exc}") from exc
    if completed.returncode:
        reason = (completed.stderr or completed.stdout).strip()
        # A missing Run key is an empty inventory, not a failed inspection.
        if registry and completed.returncode == 1 and any(text in reason.casefold() for text in (
                "unable to find the specified registry key or value", "找不到指定的注册表项或值")):
            return ""
        raise ValueError(f"{args[0]} exited {completed.returncode}: {reason}")
    return completed.stdout


def dns_check() -> tuple[str, str]:
    results: queue.Queue[tuple[str, str]] = queue.Queue()

    def lookup() -> None:
        try:
            socket.gethostbyname("example.com")
            results.put(("FAIL", "DNS lookup of example.com succeeded"))
        except socket.gaierror:
            results.put(("PASS", "DNS lookup failed"))
        except Exception as exc:
            results.put(("LOOK", f"DNS lookup unavailable: {exc}"))

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(10)
    if thread.is_alive():
        return "LOOK", "DNS lookup timed out after 10 s"
    try:
        return results.get_nowait()
    except queue.Empty:
        return "LOOK", "DNS lookup returned no result"


def network(windows: bool) -> tuple[str, str]:
    states, details = [], []
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            states.append("FAIL")
            details.append("TCP connection to 1.1.1.1:53 succeeded")
    except OSError:
        details.append("TCP probe failed")
    state, detail = dns_check()
    states.append(state)
    details.append(detail)
    if windows:
        try:
            adapters = command(["powershell", "-NoProfile", "-Command",
                                "Get-NetAdapter -ErrorAction Stop | Where-Object Status -eq 'Up' | "
                                "Select-Object -ExpandProperty Name"]).strip()
            if adapters:
                states.append("FAIL")
                details.append("Up adapters: " + ", ".join(adapters.splitlines()))
            else:
                details.append("no Up adapters")
        except ValueError as exc:
            states.append("LOOK")
            details.append(str(exc))
    return ("FAIL" if "FAIL" in states else "LOOK" if "LOOK" in states else "PASS",
            "; ".join(details))


def process_name(name: str) -> str:
    return name.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")


def agent_processes(windows: bool, allowed: list[str]) -> tuple[str, str]:
    processes = []
    if windows:
        rows = [row for row in csv.reader(io.StringIO(command(["tasklist", "/FO", "CSV", "/NH"]))) if row]
        if not rows:
            raise ValueError("tasklist returned an empty process list")
        for row in rows:
            if len(row) < 2 or not row[1].isdigit():
                raise ValueError("cannot parse tasklist process name/PID")
            if int(row[1]) != os.getpid() or "python" not in process_name(row[0]):
                processes.append(f"{row[0]} (PID {row[1]})")
        names = [entry.rsplit(" (PID ", 1)[0] for entry in processes]
    else:
        names = command(["ps", "-eo", "comm="]).splitlines()
        # Remove exactly one occurrence of our actual command name, not every Python process.
        own = command(["ps", "-p", str(os.getpid()), "-o", "comm="]).strip()
        if not own or own not in names:
            raise ValueError("cannot identify the preflight process in ps output")
        names.remove(own)
        processes = names
    allow = {process_name(name) for name in allowed}
    found = [entry for name, entry in zip(names, processes)
             if process_name(name) not in allow and any(agent in process_name(name) for agent in AGENTS)]
    return ("FAIL", "; ".join(found)) if found else ("PASS", "no matching agent processes")


def scheduled_tasks(windows: bool) -> tuple[str, str]:
    if not windows:
        return "LOOK", "check by hand"
    rows = list(csv.reader(io.StringIO(command(["schtasks", "/query", "/fo", "CSV", "/v"]))))
    if not rows or len(rows[0]) < 12 or rows[0][1].strip().casefold() not in (
            "taskname", "任务名", "任务名称"):
        raise ValueError("cannot parse schtasks CSV header")
    entries = []
    for row in rows[1:]:
        if not row or row == rows[0]:
            continue
        if len(row) < 12 or not row[1].startswith("\\"):
            raise ValueError("cannot parse schtasks task row")
        state = row[11].strip().casefold()
        if state in ("disabled", "已禁用", "禁用"):
            continue
        if state not in ("enabled", "已启用", "启用"):
            raise ValueError(f"unknown scheduled task state: {row[1]}: {row[11]}")
        if not row[1].casefold().startswith("\\microsoft\\"):
            entries.append(f"{row[1]}: {row[8]}")
    if not entries:
        return "PASS", "no enabled non-Microsoft tasks"
    details = "; ".join(entries[:40])
    if len(entries) > 40:
        details += f"; {len(entries) - 40} more tasks (showing at most 40)"
    return "LOOK", details


def startup_items(windows: bool) -> tuple[str, str]:
    if not windows:
        return "LOOK", "check by hand"
    entries = []
    for key in RUN_KEYS:
        try:
            for line in command(["reg", "query", key], registry=True).splitlines():
                if not line.strip() or line.lstrip().startswith("HKEY_"):
                    continue
                parts = re.split(r"\s+(REG_\w+)(?:\s+|$)", line.strip(), maxsplit=1)
                if len(parts) != 3:
                    raise ValueError(f"cannot parse Run entry: {line.strip()}")
                entries.append(f"{key}: {parts[0]} = {parts[2]}")
        except ValueError as exc:
            entries.append(f"{key}: {exc}")
    for variable in ("APPDATA", "ProgramData"):
        base = os.environ.get(variable)
        if not base:
            entries.append(f"{variable} unavailable; check Startup folder by hand")
            continue
        folder = Path(base) / "Microsoft/Windows/Start Menu/Programs/Startup"
        try:
            if os.path.exists(folder):
                entries.extend(str(item) for item in sorted(folder.iterdir()))
        except OSError as exc:
            entries.append(f"{folder}: {exc}")
    return ("LOOK", "; ".join(entries)) if entries else ("PASS", "no startup items")


def signing_drive() -> tuple[str, str]:
    if os.path.exists("H:\\"):
        return "LOOK", "USB already inserted; the checks above should be done before inserting it"
    return "PASS", "H:\\ absent"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow", nargs="+", action="extend", default=[], metavar="NAME")
    args = parser.parse_args(argv)
    windows = platform.system() == "Windows"
    checks = (("network", lambda: network(windows)),
              ("agent processes", lambda: agent_processes(windows, args.allow)),
              ("scheduled tasks", lambda: scheduled_tasks(windows)),
              ("startup items", lambda: startup_items(windows)),
              ("signing drive", signing_drive))
    failed = False
    print("check | result (PASS / FAIL / LOOK) | details")
    for name, check in checks:
        try:
            result, details = check()
        except (OSError, ValueError, csv.Error) as exc:
            result, details = "LOOK", str(exc)
        failed |= result == "FAIL"
        details = " ".join(details.split()).replace("|", "\\|")
        print(f"{name} | {result} | {details}")
    print("不要插入 U 盘：先处理 FAIL 项" if failed else "可以插入 U 盘签发（LOOK 项请人工确认）")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
