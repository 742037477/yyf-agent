"""Cross-process file locks: one OBS or anchor appender at a time; at most N concurrent model calls (T90)."""
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _try(f) -> bool:
    try:
        if os.name == "nt":
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:   # held by another process
        return False


def _unlock(f) -> None:
    if os.name == "nt":
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def held(paths, timeout_s=None):
    """Hold the first free lock file among paths (one path = mutex, N paths = N slots); wait up to timeout_s (None = forever)."""
    paths = [Path(p) for p in paths]
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    deadline, pause = None if timeout_s is None else time.monotonic() + timeout_s, 0.005
    while True:
        for path in random.sample(paths, len(paths)):
            f = path.open("a+b")
            if _try(f):
                try:
                    yield path
                finally:
                    _unlock(f)
                    f.close()
                return
            f.close()
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"lock {paths[0].name} still busy after {timeout_s}s")
        time.sleep(random.uniform(pause / 2, pause))
        pause = min(pause * 1.5, 0.5)


def mutex(path, timeout_s=120):
    """Exclusive lock beside `path` (a sibling `<name>.lock` file)."""
    path = Path(path)
    return held([path.with_name(path.name + ".lock")], timeout_s)
