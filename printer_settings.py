"""Atomic saves for the monitor's existing local JSON preferences."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_settings(path, data):
    """Keep the previous settings intact if writing or replacing the file fails."""
    path = Path(path)
    safe = dict(data)
    if not safe.get("remember"):
        safe.pop("code", None)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as handle:
            temporary = handle.name
            json.dump(safe, handle, indent=1, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
