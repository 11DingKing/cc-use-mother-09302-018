"""新旧决定的结构化差异。"""
from __future__ import annotations

import difflib

from .models import Decision


def decision_diff(old: Decision, new: Decision) -> dict:
    """返回两个决定版本的差异：结论、正文行级 diff、签署人变化。"""
    body_diff = list(
        difflib.unified_diff(
            old.body.splitlines(),
            new.body.splitlines(),
            fromfile=f"decision_v{old.version}",
            tofile=f"decision_v{new.version}",
            lineterm="",
        )
    )
    old_signers, new_signers = set(old.signers), set(new.signers)
    return {
        "from_version": old.version,
        "to_version": new.version,
        "from_decision_id": old.decision_id,
        "to_decision_id": new.decision_id,
        "holding_changed": old.holding != new.holding,
        "holding": {"old": old.holding, "new": new.holding},
        "body_changed": old.body != new.body,
        "body_unified_diff": body_diff,
        "signers_added": sorted(new_signers - old_signers),
        "signers_removed": sorted(old_signers - new_signers),
        "fingerprint": {"old": old.fingerprint(), "new": new.fingerprint()},
    }
