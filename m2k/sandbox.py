"""Isolation backends for every command (worker and acceptance). Fail closed: no backend → no command."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class NoSandbox:
    """The operator's explicit opt-out: commands run unisolated (argv None = plain shell), as before."""
    name = "none"

    def argv(self, command: str, cwd: Path, network: bool, info_fd: int | None = None) -> list[str] | None:
        return None


class Unavailable(NoSandbox):
    """No backend on this computer: every command is refused instead of running unisolated."""
    name = "unavailable"

    def __init__(self, reason: str):
        self.reason = reason

    def argv(self, command, cwd, network, info_fd=None):
        raise OSError(f"no isolation backend: {self.reason}")


class Bwrap(NoSandbox):
    """Linux bubblewrap: workspace is the only writable place, system runtime read-only, nothing else
    mounted, no network unless granted, own PID namespace. Reports on info_fd once the sandbox is up."""
    name = "bwrap"

    def __init__(self, exe: str | None = None):
        self.exe = exe or shutil.which("bwrap") or "bwrap"

    def argv(self, command, cwd, network, info_fd=None):
        a = [self.exe, "--die-with-parent", "--new-session", "--unshare-all"] + (["--share-net"] if network else [])
        a += ["--info-fd", str(info_fd)] if info_fd is not None else []
        for d in ("/usr", "/etc", "/bin", "/lib", "/lib64", "/sbin"):
            if os.path.islink(d):
                a += ["--symlink", os.readlink(d), d]
            elif os.path.isdir(d):
                a += ["--ro-bind", d, d]
        a += [x for p in sorted({sys.base_prefix, sys.prefix}) if not Path(p).resolve().is_relative_to("/usr")
              for x in ("--ro-bind", p, p)]
        ws = str(Path(cwd).resolve())
        return a + ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", ws, ws, "--chdir", ws,
                    "--setenv", "HOME", "/tmp", "/bin/sh", "-c", command]

    def available(self) -> tuple[bool, str]:
        if any(Path(p).resolve() == Path("/").resolve() for p in (sys.prefix, sys.base_prefix)):
            return False, "Python is installed at /; binding it would expose the whole file system"
        if sys.platform != "linux" or not shutil.which(self.exe):
            return False, f"bubblewrap (bwrap) is not installed or this is not Linux ({sys.platform})"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = subprocess.run(self.argv("true", Path(tmp), False), capture_output=True, text=True, timeout=5)
            return r.returncode == 0, "" if r.returncode == 0 else f"bwrap probe failed: {r.stderr.strip()[:300]}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"bwrap probe failed: {e}"


def choose(setting: str) -> NoSandbox:
    """Config [sandbox] backend: auto (bwrap if it works) | bwrap | none."""
    if setting not in ("auto", "bwrap", "none"):   # fail closed on a typo
        return Unavailable(f"unknown [sandbox] backend {setting!r}; use auto, bwrap or none")
    ok, reason = (True, "") if setting == "none" else Bwrap().available()
    return NoSandbox() if setting == "none" else Bwrap() if ok else Unavailable(reason)
