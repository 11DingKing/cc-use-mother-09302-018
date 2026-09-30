"""申诉业务服务：立案、补证、状态推进、回避、签署、撤回、合并、临时措施、重开、视图。"""
from __future__ import annotations

from datetime import timedelta

from . import policy, states
from .clock import Clock
from .diffing import decision_diff
from .errors import (
    AuthorizationError,
    DeadlineError,
    IdempotencyMismatchError,
    IllegalTransitionError,
    MergeError,
    NotFoundError,
    RecusalConflictError,
    SignatureError,
    ValidationError,
)
from .identifiers import generate_case_id
from .models import (
    Case,
    Decision,
    EvidenceVersion,
    InterimMeasure,
    Party,
    RecusalRelation,
    Role,
    User,
    hash_content,
)
from .states import INTERIM_MEASURE_STATES, PRE_DECISION_STATES, ensure_transition
from .storage import IdempotencyRecord, Repository, payload_fingerprint

VALID_CHANNELS = {"online", "email", "post", "onsite"}
VALID_HOLDINGS = {"成立", "部分成立", "不成立"}


class AppealService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    # ======================= 用户与回避 =======================
    def register_user(self, user_id: str, name: str, role: str) -> User:
        try:
            user_role = Role(role)
        except ValueError as exc:
            raise ValidationError(f"未知角色：{role}") from exc
        user = User(user_id=user_id, name=name, role=user_role)
        with self.repo.lock:
            if user_id in self.repo.users:
                raise ValidationError(f"用户已存在：{user_id}")
            self.repo.users[user_id] = user
        return user

    def _user(self, user_id: str) -> User:
        user = self.repo.users.get(user_id)
        if user is None:
            raise NotFoundError(f"用户不存在：{user_id}")
        return user

    def require_role(self, user_id: str, *roles: Role) -> User:
        user = self._user(user_id)
        if user.role not in roles:
            raise AuthorizationError(f"用户 {user_id} 的角色 {user.role.value} 无权执行该操作")
        return user

    def add_recusal_relation(self, officer_id: str, record_id: str, reason: str) -> RecusalRelation:
        """登记回避关系：该人员不得参与该原业务记录相关案件的复核或决定。"""
        officer = self._user(officer_id)
        if officer.role not in (Role.REVIEWER, Role.ADMIN):
            raise ValidationError("只有学校复核人员或管理员可登记回避关系")
        if not reason or not reason.strip():
            raise ValidationError("回避原因不能为空")
        relation = RecusalRelation(officer_id=officer_id, record_id=record_id, reason=reason.strip())
        with self.repo.lock:
            self.repo.recusals[officer_id].add((record_id, reason.strip()))
        return relation

    def _assert_no_recusal(self, officer_id: str, record_id: str, action: str) -> None:
        for rid, reason in self.repo.recusals.get(officer_id, set()):
            if rid == record_id:
                raise RecusalConflictError(
                    f"{officer_id} 与原业务记录 {record_id} 存在回避关系（{reason}），不得{action}"
                )

    def _is_recused(self, officer_id: str, record_id: str) -> bool:
        return any(rid == record_id for rid, _ in self.repo.recusals.get(officer_id, set()))

    # ======================= 立案（幂等） =======================
    def file_appeal(
        self,
        *,
        appellant_user_id: str,
        record_id: str,
        title: str,
        grounds: list[str],
        other_party_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        initial_evidence: list[dict] | None = None,
    ) -> tuple[Case, bool]:
        """提起申诉。返回 (案件, 是否新建)。重复提交（同一幂等键）返回原案件，绝不生成重复案件。"""
        appellant = self.require_role(appellant_user_id, Role.PARENT, Role.ACTIVITY_OWNER)
        record_id = (record_id or "").strip()
        title = (title or "").strip()
        if not record_id:
            raise ValidationError("必须关联原业务记录编号")
        if not title:
            raise ValidationError("案件标题不能为空")
        clean_grounds = [g.strip() for g in (grounds or []) if g and g.strip()]
        if not clean_grounds:
            raise ValidationError("申诉理由至少一条")

        payload = {
            "appellant_user_id": appellant_user_id,
            "record_id": record_id,
            "title": title,
            "grounds": clean_grounds,
            "other_party_ids": sorted(other_party_ids or []),
        }
        fingerprint = payload_fingerprint(payload)
        now = self.clock()

        with self.repo.lock:
            if idempotency_key:
                existing = self.repo.find_idempotency(idempotency_key)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise IdempotencyMismatchError(
                            "幂等键已用于不同的立案请求，请使用新的幂等键"
                        )
                    return self.repo.cases[existing.case_id], False

            parties = [Party(appellant_user_id, appellant.role)]
            for pid in other_party_ids or []:
                other = self._user(pid)
                parties.append(Party(pid, other.role))

            case = Case(
                case_id=generate_case_id(self.repo, now),
                record_id=record_id,
                title=title,
                appellant_user_id=appellant_user_id,
                parties=parties,
                state="提出",
                created_at=now,
                acceptance_deadline=now + timedelta(days=policy.ACCEPTANCE_DAYS),
            )
            for idx, text in enumerate(clean_grounds, start=1):
                case.grounds.append({"seq": idx, "text": text, "added_at": now.isoformat(), "added_by": appellant_user_id})
            for ev in initial_evidence or []:
                self._append_evidence(case, ev, submitted_by=appellant_user_id, at=now)
            case.record(appellant_user_id, "filed", {"record_id": record_id, "title": title}, now)

            self.repo.cases[case.case_id] = case
            if idempotency_key:
                self.repo.save_idempotency(
                    IdempotencyRecord(idempotency_key, case.case_id, fingerprint, now.isoformat())
                )
        return case, True

    def _append_evidence(self, case: Case, ev: dict, *, submitted_by: str, at) -> EvidenceVersion:
        filename = (ev.get("filename") or "").strip()
        if not filename:
            raise ValidationError("证据文件名不能为空")
        channel = ev.get("channel", "online")
        if channel not in VALID_CHANNELS:
            raise ValidationError(f"证据渠道必须是：{'、'.join(sorted(VALID_CHANNELS))}")
        content = ev.get("content")
        sha256 = ev.get("sha256")
        if content is None and not sha256:
            raise ValidationError("证据必须提供内容或 sha256 摘要")
        if content is not None:
            sha256 = hash_content(content)
        version = len(case.evidence) + 1
        item = EvidenceVersion(
            version=version,
            filename=filename,
            sha256=sha256,
            submitted_by=submitted_by,
            submitted_at=at,
            channel=channel,
            note=(ev.get("note") or "").strip(),
            supersedes=ev.get("supersedes"),
        )
        if item.supersedes is not None:
            if not isinstance(item.supersedes, int) or not (1 <= item.supersedes < version):
                raise ValidationError("supersedes 必须指向已存在的更早版本")
        case.evidence.append(item)
        case.record(
            submitted_by,
            "evidence_added",
            {"version": version, "filename": filename, "channel": channel, "supersedes": item.supersedes},
            at,
        )
        return item

    def submit_evidence(self, case_id: str, user_id: str, evidence: dict) -> EvidenceVersion:
        """当事人或办案人员补证。始终追加新版本（邮件补证不会覆盖最初版本）。"""
        with self.repo.lock:
            case = self._case(case_id)
            user = self._user(user_id)
            is_officer = user.role in (Role.REVIEWER, Role.ADMIN)
            if not is_officer and not case.is_party(user_id):
                raise AuthorizationError("只有本案当事人可以提交材料")
            if case.state in ("已撤回", "已合并"):
                raise IllegalTransitionError(f"案件处于「{case.state}」，不能再补证")
            return self._append_evidence(case, evidence, submitted_by=user_id, at=self.clock())

    # ======================= 状态推进 =======================
    def _case(self, case_id: str) -> Case:
        case = self.repo.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return case

    def _advance(self, case: Case, target: str, actor_id: str, detail: dict) -> Case:
        ensure_transition(case.state, target)
        case.state = target
        case.record(actor_id, f"transition_{target}", detail, self.clock())
        return case

    def accept_appeal(self, case_id: str, officer_id: str, note: str = "") -> Case:
        self.require_role(officer_id, Role.REVIEWER, Role.ADMIN)
        with self.repo.lock:
            case = self._case(case_id)
            self._assert_no_recusal(officer_id, case.record_id, "受理")
            now = self.clock()
            if now > case.acceptance_deadline:
                raise DeadlineError(
                    f"已超过受理期限 {case.acceptance_deadline.isoformat()}，须走逾期处理流程"
                )
            self._advance(case, "受理", officer_id, {"note": note, "at": now.isoformat()})
            return case

    def assign_officers(self, case_id: str, admin_id: str, officer_ids: list[str]) -> Case:
        """指派复核人员；有回避关系者一律不得指派。"""
        self.require_role(admin_id, Role.ADMIN)
        if not officer_ids:
            raise ValidationError("至少指派一名复核人员")
        with self.repo.lock:
            case = self._case(case_id)
            for oid in officer_ids:
                officer = self._user(oid)
                if officer.role not in (Role.REVIEWER, Role.ADMIN):
                    raise ValidationError(f"{oid} 不是复核人员")
                self._assert_no_recusal(oid, case.record_id, "本案复核")
            case.assigned_officer_ids = list(dict.fromkeys(officer_ids))
            case.record(admin_id, "officers_assigned", {"officer_ids": case.assigned_officer_ids}, self.clock())
            return case

    def begin_investigation(self, case_id: str, officer_id: str) -> Case:
        self.require_role(officer_id, Role.REVIEWER, Role.ADMIN)
        with self.repo.lock:
            case = self._case(case_id)
            ensure_transition(case.state, "调查")
            self._assert_assigned_or_admin(case, officer_id)
            self._assert_no_recusal(officer_id, case.record_id, "调查")
            return self._advance(case, "调查", officer_id, {})

    def begin_review(self, case_id: str, officer_id: str) -> Case:
        self.require_role(officer_id, Role.REVIEWER, Role.ADMIN)
        with self.repo.lock:
            case = self._case(case_id)
            ensure_transition(case.state, "复核")
            self._assert_assigned_or_admin(case, officer_id)
            self._assert_no_recusal(officer_id, case.record_id, "复核")
            return self._advance(case, "复核", officer_id, {})

    def _assert_assigned_or_admin(self, case: Case, officer_id: str) -> None:
        user = self._user(officer_id)
        if user.role == Role.ADMIN:
            return
        if officer_id not in case.assigned_officer_ids:
            raise AuthorizationError(f"{officer_id} 未被指派到本案")

    # ======================= 决定（法定签署 + 原评审人回避） =======================
    def issue_decision(
        self,
        case_id: str,
        officer_id: str,
        *,
        holding: str,
        body: str,
        signer_ids: list[str],
    ) -> Decision:
        self.require_role(officer_id, Role.REVIEWER, Role.ADMIN)
        if holding not in VALID_HOLDINGS:
            raise ValidationError(f"结论必须是：{'、'.join(sorted(VALID_HOLDINGS))}")
        if not body or not body.strip():
            raise ValidationError("决定正文不能为空")
        signer_ids = list(dict.fromkeys(signer_ids or []))
        if len(signer_ids) < policy.REQUIRED_SIGNATURES:
            raise SignatureError(f"作出决定至少需要 {policy.REQUIRED_SIGNATURES} 名签署人，当前 {len(signer_ids)} 名")
        with self.repo.lock:
            case = self._case(case_id)
            self._assert_assigned_or_admin(case, officer_id)
            for sid in signer_ids:
                signer = self._user(sid)
                if signer.role not in (Role.REVIEWER, Role.ADMIN):
                    raise SignatureError(f"签署人 {sid} 不具备签署资格")
                self._assert_no_recusal(sid, case.record_id, "签署决定")
            # 重开后的案件：决定必须经由「复核」
            ensure_transition(case.state, "决定")
            now = self.clock()
            version = len(case.decisions) + 1
            previous = case.current_decision
            decision = Decision(
                decision_id=f"{case.case_id}-D{version}",
                version=version,
                issued_at=now,
                issued_by=officer_id,
                signers=signer_ids,
                holding=holding,
                body=body.strip(),
                supersedes_decision_id=previous.decision_id if previous else None,
            )
            case.decisions.append(decision)
            case.state = "决定"
            case.reopen_deadline = now + timedelta(days=policy.REOPEN_DAYS)
            # 决定作出，未决临时措施自动到期
            for measure in case.measures:
                if measure.status in ("requested", "granted"):
                    measure.status = "expired"
            case.record(
                officer_id,
                "decision_issued",
                {"decision_id": decision.decision_id, "version": version, "holding": holding, "signers": signer_ids},
                now,
            )
            return decision

    # ======================= 撤回 =======================
    def withdraw_appeal(self, case_id: str, user_id: str, reason: str) -> Case:
        with self.repo.lock:
            case = self._case(case_id)
            user = self._user(user_id)
            if user.role not in (Role.PARENT, Role.ACTIVITY_OWNER) or not case.is_party(user_id):
                raise AuthorizationError("只有申诉当事人可以撤回")
            if case.state not in PRE_DECISION_STATES:
                raise IllegalTransitionError(f"案件处于「{case.state}」，不能撤回")
            if not reason or not reason.strip():
                raise ValidationError("撤回原因不能为空")
            now = self.clock()
            case.state = "已撤回"
            case.withdrawal = {"by": user_id, "at": now.isoformat(), "reason": reason.strip()}
            for measure in case.measures:
                if measure.status in ("requested", "granted"):
                    measure.status = "expired"
            case.record(user_id, "withdrawn", {"reason": reason.strip()}, now)
            return case

    # ======================= 案件合并 =======================
    def merge_cases(self, admin_id: str, lead_case_id: str, subordinate_ids: list[str]) -> Case:
        """将同原业务记录、决定前的从案件并入主案件；从案件置为「已合并」。"""
        self.require_role(admin_id, Role.ADMIN)
        if not subordinate_ids:
            raise ValidationError("至少指定一个被合并案件")
        if lead_case_id in subordinate_ids:
            raise MergeError("主案件不能同时是从案件")
        with self.repo.lock:
            lead = self._case(lead_case_id)
            if lead.state == "已合并":
                raise MergeError("主案件自身已被合并")
            if lead.state not in PRE_DECISION_STATES:
                raise MergeError(f"主案件处于「{lead.state}」，仅决定前案件可以合并")
            now = self.clock()
            for sid in subordinate_ids:
                sub = self._case(sid)
                if sub.state == "已合并":
                    raise MergeError(f"案件 {sid} 已被合并，不能重复合并")
                if sub.record_id != lead.record_id:
                    raise MergeError(f"案件 {sid} 关联的原业务记录不同，不能合并")
                if sub.state not in PRE_DECISION_STATES:
                    raise MergeError(f"案件 {sid} 已进入「{sub.state}」，不能合并")
                if sid in lead.merged_case_ids:
                    raise MergeError(f"案件 {sid} 已在主案件中")
            # 全部校验通过后落库
            for sid in subordinate_ids:
                sub = self.repo.cases[sid]
                sub.state = "已合并"
                sub.merged_into = lead.case_id
                # 证据并入主案件并重新编号（内容摘要与来源保留，不覆盖任何既有版本）
                for ev in list(sub.evidence):
                    renumbered = EvidenceVersion(
                        version=len(lead.evidence) + 1,
                        filename=ev.filename,
                        sha256=ev.sha256,
                        submitted_by=ev.submitted_by,
                        submitted_at=ev.submitted_at,
                        channel=ev.channel,
                        note=f"[合并自 {sid}] {ev.note}".strip(),
                        supersedes=None,
                    )
                    lead.evidence.append(renumbered)
                lead.grounds.extend(sub.grounds)
                lead.merged_case_ids.append(sid)
                lead.record(admin_id, "case_merged_in", {"subordinate_case_id": sid}, now)
                sub.record(admin_id, "merged_into_lead", {"lead_case_id": lead.case_id}, now)
            lead.record(admin_id, "merge_completed", {"subordinate_ids": list(subordinate_ids)}, now)
            return lead

    # ======================= 临时措施 =======================
    def request_interim_measure(self, case_id: str, user_id: str, content: str) -> InterimMeasure:
        with self.repo.lock:
            case = self._case(case_id)
            user = self._user(user_id)
            is_officer = user.role in (Role.REVIEWER, Role.ADMIN)
            if not is_officer and not case.is_party(user_id):
                raise AuthorizationError("只有本案当事人可以申请临时措施")
            if case.state not in INTERIM_MEASURE_STATES:
                raise IllegalTransitionError(f"案件处于「{case.state}」，不能申请临时措施")
            if not content or not content.strip():
                raise ValidationError("临时措施内容不能为空")
            now = self.clock()
            measure = InterimMeasure(
                measure_id=f"{case.case_id}-M{len(case.measures) + 1}",
                requested_by=user_id,
                requested_at=now,
                status="requested",
                content=content.strip(),
            )
            case.measures.append(measure)
            case.record(user_id, "interim_requested", {"measure_id": measure.measure_id}, now)
            return measure

    def decide_interim_measure(
        self, case_id: str, officer_id: str, measure_id: str, grant: bool, remark: str = ""
    ) -> InterimMeasure:
        self.require_role(officer_id, Role.REVIEWER, Role.ADMIN)
        with self.repo.lock:
            case = self._case(case_id)
            self._assert_assigned_or_admin(case, officer_id)
            self._assert_no_recusal(officer_id, case.record_id, "处理临时措施")
            measure = next((m for m in case.measures if m.measure_id == measure_id), None)
            if measure is None:
                raise NotFoundError(f"临时措施不存在：{measure_id}")
            if measure.status != "requested":
                raise IllegalTransitionError(f"临时措施处于「{measure.status}」，不能再裁定")
            now = self.clock()
            measure.status = "granted" if grant else "denied"
            measure.decided_by = officer_id
            measure.decided_at = now
            case.record(
                officer_id,
                "interim_decided",
                {"measure_id": measure_id, "granted": grant, "remark": remark},
                now,
            )
            return measure

    def lift_interim_measure(self, case_id: str, officer_id: str, measure_id: str) -> InterimMeasure:
        self.require_role(officer_id, Role.REVIEWER, Role.ADMIN)
        with self.repo.lock:
            case = self._case(case_id)
            measure = next((m for m in case.measures if m.measure_id == measure_id), None)
            if measure is None:
                raise NotFoundError(f"临时措施不存在：{measure_id}")
            if measure.status != "granted":
                raise IllegalTransitionError("只有已批准的临时措施可以解除")
            measure.status = "lifted"
            measure.decided_by = officer_id
            measure.decided_at = self.clock()
            case.record(officer_id, "interim_lifted", {"measure_id": measure_id}, measure.decided_at)
            return measure

    # ======================= 决定重开 =======================
    def reopen_case(self, case_id: str, user_id: str, reason: str) -> Case:
        """决定后在法定期限内申请重开；原决定保留，案件回到「重开」并须重新经「复核」。"""
        if not reason or not reason.strip():
            raise ValidationError("重开理由不能为空")
        with self.repo.lock:
            case = self._case(case_id)
            user = self._user(user_id)
            is_officer = user.role in (Role.REVIEWER, Role.ADMIN)
            if not is_officer and not case.is_party(user_id):
                raise AuthorizationError("只有本案当事人或办公室可以申请重开")
            if case.state != "决定":
                raise IllegalTransitionError(f"案件处于「{case.state}」，只有已定案件可以重开")
            if case.reopen_deadline is not None and self.clock() > case.reopen_deadline:
                raise DeadlineError(f"已超过重开期限 {case.reopen_deadline.isoformat()}")
            decision = case.current_decision
            ensure_transition("决定", "重开")
            now = self.clock()
            case.state = "重开"
            case.reopened_from_decision_id = decision.decision_id if decision else None
            case.record(
                user_id,
                "reopened",
                {"reason": reason.strip(), "from_decision_id": case.reopened_from_decision_id},
                now,
            )
            return case

    # ======================= 查询视图 =======================
    def get_case(self, case_id: str, user_id: str) -> dict:
        """当事人视角：只能看到自己的案件与自己提交的材料。"""
        user = self._user(user_id)
        with self.repo.lock:
            case = self._case(case_id)
            if user.role == Role.ADMIN:
                return self._admin_view(case)
            if user.role in (Role.REVIEWER,):
                if self._is_recused(user_id, case.record_id):
                    raise AuthorizationError("存在回避关系，不得查看本案")
                if user_id not in case.assigned_officer_ids:
                    raise AuthorizationError("复核人员只能查看被指派的案件")
                return self._officer_view(case, user_id)
            if not case.is_party(user_id):
                raise AuthorizationError("当事人只能查看自己的案件")
            return self._party_view(case, user_id)

    def list_cases(self, user_id: str) -> list[dict]:
        user = self._user(user_id)
        with self.repo.lock:
            cases = list(self.repo.cases.values())
            if user.role == Role.ADMIN:
                return [c.to_summary_dict() for c in sorted(cases, key=lambda c: c.case_id)]
            if user.role == Role.REVIEWER:
                visible = [
                    c
                    for c in cases
                    if user_id in c.assigned_officer_ids and not self._is_recused(user_id, c.record_id)
                ]
                return [c.to_summary_dict() for c in sorted(visible, key=lambda c: c.case_id)]
            mine = [c for c in cases if c.is_party(user_id)]
            return [c.to_summary_dict() for c in sorted(mine, key=lambda c: c.case_id)]

    def _party_view(self, case: Case, user_id: str) -> dict:
        data = case.to_summary_dict()
        data["view"] = "party"
        data["grounds"] = list(case.grounds)
        # 当事人只看到自己的材料；邮件补证等他方材料对其隔离
        data["evidence"] = [e.to_dict() for e in case.evidence if e.submitted_by == user_id]
        data["measures"] = [m.to_dict() for m in case.measures if m.requested_by == user_id]
        data["decisions"] = [
            {**d.to_dict(), "sha256": d.fingerprint()}
            for d in case.decisions
        ]
        # 时间线隐去内部人员信息，只保留与该当事人相关的条目
        data["timeline"] = [
            t.to_dict()
            for t in case.timeline
            if t.actor_id == user_id or t.kind in {"transition_受理", "transition_调查", "transition_复核", "transition_决定", "decision_issued"}
        ]
        return data

    def _officer_view(self, case: Case, user_id: str) -> dict:
        data = case.to_detail_dict()
        data["view"] = "officer"
        data["recusal_flags"] = [
            {"officer_id": oid, "recused": self._is_recused(oid, case.record_id)}
            for oid in case.assigned_officer_ids
        ]
        return data

    def _admin_view(self, case: Case) -> dict:
        """管理员视角：完整时间线、全部证据版本、回避标记。"""
        data = case.to_detail_dict()
        data["view"] = "admin"
        data["recusal_relations"] = []
        for oid, rels in self.repo.recusals.items():
            for rid, reason in rels:
                if rid == case.record_id:
                    data["recusal_relations"].append({"officer_id": oid, "record_id": rid, "reason": reason})
        data["decisions"] = [{**d.to_dict(), "sha256": d.fingerprint()} for d in case.decisions]
        return data

    def decision_history_diff(self, case_id: str, admin_id: str) -> dict:
        """管理员查看新旧决定差异（重开前后）。"""
        self.require_role(admin_id, Role.ADMIN)
        with self.repo.lock:
            case = self._case(case_id)
            if len(case.decisions) < 2:
                raise NotFoundError("本案只有一个决定版本，暂无差异可比")
            diffs = [
                decision_diff(case.decisions[i - 1], case.decisions[i])
                for i in range(1, len(case.decisions))
            ]
            return {
                "case_id": case_id,
                "current_version": case.current_decision.version,
                "diffs": diffs,
            }

    def recusal_check(self, admin_id: str, record_id: str) -> dict:
        """管理员按原业务记录查看全部回避关系。"""
        self.require_role(admin_id, Role.ADMIN)
        with self.repo.lock:
            relations = []
            for oid, rels in self.repo.recusals.items():
                for rid, reason in rels:
                    if rid == record_id:
                        relations.append({"officer_id": oid, "record_id": rid, "reason": reason})
            related_cases = [c.case_id for c in self.repo.cases.values() if c.record_id == record_id]
            return {"record_id": record_id, "recusals": relations, "case_ids": sorted(related_cases)}
