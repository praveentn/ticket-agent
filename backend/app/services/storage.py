"""Object storage abstraction. Local volume for development; swap for S3/Azure Blob/GCS in production."""

import uuid
from pathlib import Path

from app.config import settings


class LocalObjectStore:
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, data: bytes, prefix: str, filename: str) -> str:
        safe = "".join(ch for ch in filename if ch.isalnum() or ch in "._-")[-80:] or "file"
        key = f"{prefix}/{uuid.uuid4().hex}_{safe}"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("invalid storage key")
        return path.read_bytes()


store = LocalObjectStore(settings.object_store_dir)
