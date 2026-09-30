"""内存存储：用户、回避关系、案件与幂等键。线程安全，可整体替换为数据库实现。"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict


def payload_fingerprint(payload: dict) -> str:
    """对立案请求体取稳定指纹，用于检测同一幂等键的不同内容。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyRecord:
    __slots__ = ("key", "case_id", "fingerprint", "created_at")

    def __init__(self, key: str, case_id: str, fingerprint: str, created_at: str) -> None:
        self.key = key
        self.case_id = case_id
        self.fingerprint = fingerprint
        self.created_at = created_at


class Repository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.users: dict[str, object] = {}
        self.cases: dict[str, object] = {}
        self.recusals: dict[str, set[tuple[str, str]]] = defaultdict(set)  # officer_id -> {(record_id, reason)}
        self.idempotency: dict[str, IdempotencyRecord] = {}
        self._case_seq = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def next_case_serial(self) -> int:
        self._case_seq += 1
        return self._case_seq

    # ---- 幂等键 ----
    def find_idempotency(self, key: str) -> IdempotencyRecord | None:
        return self.idempotency.get(key)

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        self.idempotency[record.key] = record

    def reset(self) -> None:
        with self._lock:
            self.users.clear()
            self.cases.clear()
            self.recusals.clear()
            self.idempotency.clear()
            self._case_seq = 0
