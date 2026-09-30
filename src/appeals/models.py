"""领域模型：用户、案件、证据版本、理由、回避关系、决定与时间线。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .errors import ValidationError


class Role(str, Enum):
    PARENT = "学生家长"
    REVIEWER = "学校复核人员"
    ACTIVITY_OWNER = "活动负责人"
    ADMIN = "申诉办公室管理员"


# 可被指派为案件复核/签署人的角色
OFFICER_ROLES = {Role.REVIEWER, Role.ADMIN}


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: Role

    def to_dict(self) -> dict:
        return {"user_id": self.user_id, "name": self.name, "role": self.role.value}


@dataclass(frozen=True)
class Party:
    """案件当事人（只能看到自己的材料）。"""

    user_id: str
    role: Role

    def to_dict(self) -> dict:
        return {"user_id": self.user_id, "role": self.role.value}


@dataclass
class RecusalRelation:
    """回避关系：某复核人员与某原业务记录存在关联，不得参与该记录相关案件。"""

    officer_id: str
    record_id: str
    reason: str

    def to_dict(self) -> dict:
        return {"officer_id": self.officer_id, "record_id": self.record_id, "reason": self.reason}


@dataclass
class EvidenceVersion:
    """证据材料的一个不可变版本。补证只追加新版本，绝不覆盖。"""

    version: int
    filename: str
    sha256: str
    submitted_by: str
    submitted_at: datetime
    channel: str  # online / email / post / onsite
    note: str = ""
    supersedes: int | None = None  # 被本版本补充/替换的前序版本号

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "filename": self.filename,
            "sha256": self.sha256,
            "submitted_by": self.submitted_by,
            "submitted_at": self.submitted_at.isoformat(),
            "channel": self.channel,
            "note": self.note,
            "supersedes": self.supersedes,
        }


@dataclass
class TimelineEntry:
    """时间线条目：所有状态动作与材料事件的不可变审计记录。"""

    seq: int
    at: datetime
    actor_id: str
    kind: str
    detail: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "actor_id": self.actor_id,
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass
class Decision:
    """一次正式决定。重开后再决定会产生新版本，旧版本永久保留。"""

    decision_id: str
    version: int
    issued_at: datetime
    issued_by: str
    signers: list[str]
    holding: str  # 成立 / 部分成立 / 不成立
    body: str
    supersedes_decision_id: str | None = None

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"holding": self.holding, "body": self.body, "signers": sorted(self.signers)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "version": self.version,
            "issued_at": self.issued_at.isoformat(),
            "issued_by": self.issued_by,
            "signers": list(self.signers),
            "holding": self.holding,
            "body": self.body,
            "supersedes_decision_id": self.supersedes_decision_id,
        }


@dataclass
class InterimMeasure:
    measure_id: str
    requested_by: str
    requested_at: datetime
    status: str  # requested / granted / denied / lifted / expired
    content: str
    decided_by: str | None = None
    decided_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "measure_id": self.measure_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at.isoformat(),
            "status": self.status,
            "content": self.content,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


def _require(value: str, field_name: str) -> str:
    if not value or not str(value).strip():
        raise ValidationError(f"{field_name} 不能为空")
    return str(value).strip()


@dataclass
class Case:
    case_id: str
    record_id: str  # 原业务记录（作品使用事项）编号
    title: str
    appellant_user_id: str  # 提起申诉的当事人
    parties: list[Party]
    state: str
    created_at: datetime
    acceptance_deadline: datetime
    reopen_deadline: datetime | None = None
    evidence: list[EvidenceVersion] = field(default_factory=list)
    grounds: list[dict] = field(default_factory=list)  # 理由，追加式
    timeline: list[TimelineEntry] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    measures: list[InterimMeasure] = field(default_factory=list)
    assigned_officer_ids: list[str] = field(default_factory=list)
    merged_into: str | None = None  # 被合并后的主案件
    merged_case_ids: list[str] = field(default_factory=list)  # 主案件吸收的从案件
    withdrawal: dict | None = None
    reopened_from_decision_id: str | None = None
    _seq: int = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record(self, actor_id: str, kind: str, detail: dict, at: datetime) -> TimelineEntry:
        entry = TimelineEntry(seq=self.next_seq(), at=at, actor_id=actor_id, kind=kind, detail=detail)
        self.timeline.append(entry)
        return entry

    def is_party(self, user_id: str) -> bool:
        return any(p.user_id == user_id for p in self.parties) or self.appellant_user_id == user_id

    @property
    def current_decision(self) -> Decision | None:
        return self.decisions[-1] if self.decisions else None

    def to_summary_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "record_id": self.record_id,
            "title": self.title,
            "state": self.state,
            "appellant_user_id": self.appellant_user_id,
            "created_at": self.created_at.isoformat(),
            "acceptance_deadline": self.acceptance_deadline.isoformat(),
            "reopen_deadline": self.reopen_deadline.isoformat() if self.reopen_deadline else None,
            "merged_into": self.merged_into,
        }

    def to_detail_dict(self) -> dict:
        data = self.to_summary_dict()
        data.update(
            {
                "parties": [p.to_dict() for p in self.parties],
                "assigned_officer_ids": list(self.assigned_officer_ids),
                "grounds": list(self.grounds),
                "evidence": [e.to_dict() for e in self.evidence],
                "decisions": [d.to_dict() for d in self.decisions],
                "measures": [m.to_dict() for m in self.measures],
                "merged_case_ids": list(self.merged_case_ids),
                "withdrawal": self.withdrawal,
                "reopened_from_decision_id": self.reopened_from_decision_id,
                "timeline": [t.to_dict() for t in self.timeline],
            }
        )
        return data


def hash_content(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()
