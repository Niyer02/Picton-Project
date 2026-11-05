"""Atomic artifact writes, file checksums, and UTC provenance timestamps."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def save(path, content):
    """Replace a file only after the complete payload has been written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def save_json(path, value):
    save(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def value_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
