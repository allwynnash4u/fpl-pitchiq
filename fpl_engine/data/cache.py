from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class CacheEntry:
    key: str
    data: Any
    fetched_at: datetime
    sha256: str

    def is_fresh(self, ttl: timedelta, now: datetime | None = None) -> bool:
        return (now or utc_now()) - self.fetched_at <= ttl


class DiskJsonCache:
    """Small atomic JSON cache with checksums to detect local corruption."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_name(key: str) -> str:
        readable = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)[:70]
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        return f"{readable}-{digest}.json"

    def path_for(self, key: str) -> Path:
        return self.directory / self._safe_name(key)

    @staticmethod
    def _payload_hash(data: Any) -> str:
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get(self, key: str) -> CacheEntry | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            data = envelope["data"]
            expected = envelope["sha256"]
            if self._payload_hash(data) != expected:
                return None
            return CacheEntry(
                key=key,
                data=data,
                fetched_at=parse_timestamp(envelope["fetched_at"]),
                sha256=expected,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def put(self, key: str, data: Any, fetched_at: datetime | None = None) -> CacheEntry:
        timestamp = fetched_at or utc_now()
        digest = self._payload_hash(data)
        envelope = {
            "key": key,
            "fetched_at": timestamp.isoformat(),
            "sha256": digest,
            "data": data,
        }
        target = self.path_for(key)
        handle, temp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(envelope, stream, separators=(",", ":"), ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return CacheEntry(key=key, data=data, fetched_at=timestamp, sha256=digest)

