import hashlib
import json
from pathlib import Path

from . import config


class Cache:
    def __init__(self, directory: Path | None = None, enabled: bool | None = None):
        cfg = config.settings
        self.dir = directory or cfg.cache_dir
        self.enabled = cfg.cache_enabled if enabled is None else enabled

    @staticmethod
    def key(*parts: bytes | str) -> str:
        h = hashlib.sha256()
        for part in parts:
            b = part if isinstance(part, bytes) else part.encode("utf-8")
            h.update(len(b).to_bytes(8, "big"))
            h.update(b)
        return h.hexdigest()

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        p = self.path(key)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def put(self, key: str, record: dict) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(key)
        p.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return p
