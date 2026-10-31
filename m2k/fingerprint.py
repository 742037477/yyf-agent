"""Pin the framework code and input files without recording their contents (T40)."""
import hashlib
from pathlib import Path

from .trust import canonical, release_digest


def framework_fingerprint(code_dir, files, contents=None) -> dict:
    """Only code/files enter the digest; contents reuses bytes already read at startup.

    code_dir identifies the source for later rechecks, which always read the files afresh.
    """
    hashes = {}
    for file in files:
        path = Path(file).absolute()
        try:
            raw = contents[path] if contents is not None and path in contents else path.read_bytes()
            hashes[str(path)] = hashlib.sha256(raw).hexdigest()
        except FileNotFoundError:
            hashes[str(path)] = None
    body = {"code": release_digest(code_dir), "files": hashes}
    return body | {"digest": hashlib.sha256(canonical(body)).hexdigest(), "code_dir": str(Path(code_dir).absolute())}
