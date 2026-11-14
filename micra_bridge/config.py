"""Private local configuration. Never log credentials or raw API responses."""

import json
import os
import tempfile
from pathlib import Path


def save_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".micra-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path: Path) -> dict:
    value = json.loads(path.read_text())
    for name in ("username", "password", "bridge_token"):
        if not isinstance(value.get(name), str) or not value[name]:
            raise ValueError(f"Missing configuration: {name}")
    if len(value["bridge_token"]) < 32 or not value["bridge_token"].isascii():
        raise ValueError("Invalid bridge token; run configure again")
    return value
