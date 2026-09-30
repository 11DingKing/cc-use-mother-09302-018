"""申诉后端业务规则测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from appeals.app import build_app, seed_demo
from appeals.clock import MovableClock
from appeals.errors import (
    AuthorizationError,
    DeadlineError,
    IdempotencyMismatchError,
    IllegalTransitionError,
    MergeError,
    RecusalConflictError,
    SignatureError,
    ValidationError,
)
from appeals.storage import Repository
from appeals import create_appeal_service
from domain_contract.validator import load_contract


class AppealServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MovableClock()
        self.service, self.repo, self.contract = build_app(clock=self.clock)
        s = self.service
        s.register_user("parent1", "李家长", "学生家长")
        s.register_user("parent2", "王家长", "学生家长")
        s.register_user("owner1", "周指导", "活动负责人")
        s.register_user("rev_orig", "赵评审", "学校复核人员")  # 原决定评审人
        s.register_user("rev_a", "钱复核", "学校复核人员")
        s.register_user("rev_b", "孙复核", "学校复核人员")
        s.register_user("rev_c", "李复核", "学校复核人员")
        s.register_user("admin1", "管理员", "申诉办公室管理员")
        s.add_recusal_relation("rev_orig", "WORK-2026-018", "原作品使用决定评审人")

    def file_case(self, idempotency_key: str | None = "k-1", **overrides):
        kwargs = dict(
            appellant_user_id="parent1",
            record_id="WORK-2026-018",
            title="作品使用异议",
            grounds=["未征得同意使用作品"],
            other_party_ids=["owner1"],
            idempotency_key=idempotency_key,
        )
        kwargs.update(overrides)
        case, created = self.service.file_appeal(**kwargs)
        self.last_case_id = case.case_id
        return case, created

    def advance_to_review(self, case_id: str):
        """提出 -> 受理 -> 指派 -> 调查 -> 复核。"""
        s = self.service
        s.accept_appeal(case_id, "admin1")
        s.assign_officers(case_id, "admin1", ["rev_a", "rev_b", "rev_c"])
        s.begin_investigation(case_id, "rev_a")
        s.begin_review(case_id, "rev_a")

    def issue_first_decision(self, case_id: str | None = None, holding="不成立", body="原决定正文\n维持使用。"):
        return self.service.issue_decision(
            case_id or self.last_case_id,
            "rev_a",
            holding=holding,
            body=body,
            signer_ids=["rev_a", "rev_b", "rev_c"],
        )


class FilingIdempotencyTest(AppealServiceTestBase):
    def test_retry_with_same_idempotency_key_does_not_duplicate(self) -> None:
        case1, created1 = self.file_case()
        case2, created2 = self.file_case()
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(case1.case_id, case2.case_id)
        self.assertEqual(len(self.repo.cases), 1)

    def test_same_key_different_payload_rejected(self) -> None:
        self.file_case()
        with self.assertRaises(IdempotencyMismatchError):
            self.file_case(title="被篡改的标题")

    def test_concurrent_retries_still_single_case(self) -> None:
        import concurrent.futures

        def attempt(_):
            return self.file_case()[0].case_id

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = set(pool.map(attempt, range(8)))
        self.assertEqual(ids, {next(iter(ids))})
        self.assertEqual(len(self.repo.cases), 1)

    def test_without_key_retries_create_separate_cases(self) -> None:
        c1, _ = self.file_case(idempotency_key=None)
        c2, _ = self.file_case(idempotency_key=None)
        self.assertNotEqual(c1.case_id, c2.case_id)

    def test_filing_requires_record_link_and_grounds(self) -> None:
        with self.assertRaises(ValidationError):
            self.file_case(record_id="  ", idempotency_key="x")
        with self.assertRaises(ValidationError):
            self.file_case(grounds=[], idempotency_key="y")

    def test_case_carries_acceptance_deadline(self) -> None:
        case, _ = self.file_case()
        self.assertEqual((case.acceptance_deadline - case.created_at).days, 5)


class EvidenceVersioningTest(AppealServiceTestBase):
    def test_email_supplement_appends_never_overwrites(self) -> None:
        case, _ = self.file_case(
            initial_evidence=[{"filename": "p.jpg", "content": "最初版本", "channel": "online"}]
        )
        self.service.submit_evidence(
            case.case_id,
            "parent1",
            {"filename": "p.jpg", "content": "邮件覆盖包", "channel": "email", "note": "邮件补证", "supersedes": 1},
        )
        fresh = self.repo.cases[case.case_id]
        self.assertEqual([e.version for e in fresh.evidence], [1, 2])
        self.assertNotEqual(fresh.evidence[0].sha256, fresh.evidence[1].sha256)
        # 最初版本原样保留
        self.assertEqual(fresh.evidence[0].channel, "online")
        self.assertEqual(fresh.evidence[1].supersedes, 1)

    def test_supersedes_must_point_earlier_version(self) -> None:
        case, _ = self.file_case()
        with self.assertRaises(ValidationError):
            self.service.submit_evidence(
                case.case_id, "parent1", {"filename": "a", "content": "x", "supersedes": 9}
            )

    def test_third_party_cannot_submit(self) -> None:
        case, _ = self.file_case()
        with self.assertRaises(AuthorizationError):
            self.service.submit_evidence(
                case.case_id, "parent2", {"filename": "a", "content": "x"}
            )


class RecusalAndSignatureTest(AppealServiceTestBase):
    def test_original_reviewer_blocked_at_every_stage(self) -> None:
        case, _ = self.file_case()
        with self.assertRaises(RecusalConflictError):
            self.service.accept_appeal(case.case_id, "rev_orig")
        with self.assertRaises(RecusalConflictError):
            self.service.assign_officers(case.case_id, "admin1", ["rev_orig"])
        self.advance_to_review(case.case_id)
        with self.assertRaises(RecusalConflictError):
            self.service.issue_decision(
                case.case_id, "rev_a", holding="成立", body="x", signer_ids=["rev_a", "rev_b", "rev_orig"]
            )

    def test_original_reviewer_cannot_see_case(self) -> None:
        case, _ = self.file_case()
        self.service.assign_officers(case.case_id, "admin1", ["rev_a"])
        with self.assertRaises(AuthorizationError):
            self.service.get_case(case.case_id, "rev_orig")

    def test_decision_requires_three_signers(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        with self.assertRaises(SignatureError):
            self.service.issue_decision(
                case.case_id, "rev_a", holding="成立", body="x", signer_ids=["rev_a", "rev_b"]
            )

    def test_non_signer_role_rejected(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        with self.assertRaises(SignatureError):
            self.service.issue_decision(
                case.case_id, "rev_a", holding="成立", body="x",
                signer_ids=["rev_a", "rev_b", "parent1"],
            )


class LifecycleTest(AppealServiceTestBase):
    def test_happy_path_to_decision(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        d = self.issue_first_decision()
        self.assertEqual(case.state, "决定")
        self.assertEqual(d.version, 1)
        self.assertEqual((case.reopen_deadline - d.issued_at).days, 15)

    def test_illegal_transitions_rejected(self) -> None:
        case, _ = self.file_case()
        # 未经受理不能调查
        with self.assertRaises(IllegalTransitionError):
            self.service.begin_investigation(case.case_id, "rev_a")

    def test_acceptance_after_deadline_rejected(self) -> None:
        case, _ = self.file_case()
        self.clock.advance(days=6)
        with self.assertRaises(DeadlineError):
            self.service.accept_appeal(case.case_id, "admin1")

    def test_withdraw_pre_decision_and_blocks_further_evidence(self) -> None:
        case, _ = self.file_case()
        self.service.withdraw_appeal(case.case_id, "parent1", "已和解")
        self.assertEqual(case.state, "已撤回")
        with self.assertRaises(IllegalTransitionError):
            self.service.accept_appeal(case.case_id, "admin1")
        with self.assertRaises(IllegalTransitionError):
            self.service.submit_evidence(case.case_id, "parent1", {"filename": "a", "content": "x"})

    def test_withdraw_after_decision_rejected(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        self.issue_first_decision()
        with self.assertRaises(IllegalTransitionError):
            self.service.withdraw_appeal(case.case_id, "parent1", "反悔")

    def test_reopen_after_deadline_rejected(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        self.issue_first_decision()
        self.clock.advance(days=16)
        with self.assertRaises(DeadlineError):
            self.service.reopen_case(case.case_id, "parent1", "发现新证据")

    def test_reopen_must_pass_review_again(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        d1 = self.issue_first_decision(holding="不成立", body="原决定：不成立。")
        self.service.reopen_case(case.case_id, "parent1", "发现邮件补证未被审阅")
        self.assertEqual(case.state, "重开")
        # 重开后不能直接出决定，必须重新经复核
        with self.assertRaises(IllegalTransitionError):
            self.service.issue_decision(
                case.case_id, "rev_a", holding="成立", body="新决定",
                signer_ids=["rev_a", "rev_b", "rev_c"],
            )
        self.service.begin_review(case.case_id, "rev_a")
        d2 = self.service.issue_decision(
            case.case_id, "rev_a", holding="成立", body="新决定：成立。",
            signer_ids=["rev_a", "rev_b", "rev_c"],
        )
        self.assertEqual(d2.version, 2)
        self.assertEqual(d2.supersedes_decision_id, d1.decision_id)
        # 旧决定永久保留
        self.assertEqual([d.version for d in case.decisions], [1, 2])


class MergeTest(AppealServiceTestBase):
    def test_merge_same_record_predecision_cases(self) -> None:
        lead, _ = self.file_case(idempotency_key="lead")
        sub, _ = self.file_case(idempotency_key="sub", title="同一作品的另一异议")
        self.service.submit_evidence(lead.case_id, "parent1", {"filename": "L", "content": "lead-ev"})
        self.service.submit_evidence(sub.case_id, "parent1", {"filename": "S", "content": "sub-ev"})
        merged = self.service.merge_cases("admin1", lead.case_id, [sub.case_id])
        self.assertEqual(merged.state, "提出")
        self.assertIn(sub.case_id, merged.merged_case_ids)
        self.assertEqual(self.repo.cases[sub.case_id].state, "已合并")
        self.assertEqual(self.repo.cases[sub.case_id].merged_into, lead.case_id)
        # 主案件证据连续编号、无覆盖
        versions = [(e.version, e.filename) for e in merged.evidence]
        self.assertEqual(versions, [(1, "L"), (2, "S")])

    def test_merge_different_record_rejected(self) -> None:
        lead, _ = self.file_case(idempotency_key="lead")
        other, _ = self.file_case(idempotency_key="other", record_id="WORK-2026-999")
        with self.assertRaises(MergeError):
            self.service.merge_cases("admin1", lead.case_id, [other.case_id])

    def test_merge_decided_case_rejected(self) -> None:
        lead, _ = self.file_case(idempotency_key="lead")
        sub, _ = self.file_case(idempotency_key="sub")
        # 从案件已推进到决定，不可再并入
        self.advance_to_review(sub.case_id)
        self.service.issue_decision(
            sub.case_id, "rev_a", holding="不成立", body="x",
            signer_ids=["rev_a", "rev_b", "rev_c"],
        )
        with self.assertRaises(MergeError):
            self.service.merge_cases("admin1", lead.case_id, [sub.case_id])

    def test_only_admin_can_merge(self) -> None:
        lead, _ = self.file_case(idempotency_key="lead")
        sub, _ = self.file_case(idempotency_key="sub")
        with self.assertRaises(AuthorizationError):
            self.service.merge_cases("rev_a", lead.case_id, [sub.case_id])


class InterimMeasureTest(AppealServiceTestBase):
    def test_request_grant_and_expiry_on_decision(self) -> None:
        case, _ = self.file_case()
        self.service.accept_appeal(case.case_id, "admin1")
        self.service.assign_officers(case.case_id, "admin1", ["rev_a", "rev_b", "rev_c"])
        m = self.service.request_interim_measure(case.case_id, "parent1", "暂停展板使用")
        self.assertEqual(m.status, "requested")
        self.service.decide_interim_measure(case.case_id, "rev_a", m.measure_id, grant=True)
        self.assertEqual(case.measures[0].status, "granted")
        self.service.begin_investigation(case.case_id, "rev_a")
        self.service.begin_review(case.case_id, "rev_a")
        self.issue_first_decision()
        # 决定作出后临时措施自动到期
        self.assertEqual(case.measures[0].status, "expired")

    def test_request_not_allowed_at_filing(self) -> None:
        case, _ = self.file_case()
        with self.assertRaises(IllegalTransitionError):
            self.service.request_interim_measure(case.case_id, "parent1", "停止使用")

    def test_denied_then_cannot_decide_again(self) -> None:
        case, _ = self.file_case()
        self.service.accept_appeal(case.case_id, "admin1")
        m = self.service.request_interim_measure(case.case_id, "parent1", "停止使用")
        self.service.decide_interim_measure(case.case_id, "admin1", m.measure_id, grant=False)
        with self.assertRaises(IllegalTransitionError):
            self.service.decide_interim_measure(case.case_id, "admin1", m.measure_id, grant=True)


class VisibilityTest(AppealServiceTestBase):
    def test_party_sees_only_own_materials(self) -> None:
        case, _ = self.file_case()
        self.service.submit_evidence(case.case_id, "parent1", {"filename": "p", "content": "parent-material"})
        self.service.submit_evidence(case.case_id, "owner1", {"filename": "o", "content": "owner-material"})
        view = self.service.get_case(case.case_id, "parent1")
        submitters = {e["submitted_by"] for e in view["evidence"]}
        self.assertEqual(submitters, {"parent1"})

    def test_other_party_cannot_open_case(self) -> None:
        case, _ = self.file_case()
        with self.assertRaises(AuthorizationError):
            self.service.get_case(case.case_id, "parent2")

    def test_admin_sees_full_timeline(self) -> None:
        case, _ = self.file_case()
        self.service.submit_evidence(case.case_id, "parent1", {"filename": "p", "content": "x"})
        self.service.submit_evidence(case.case_id, "owner1", {"filename": "o", "content": "x"})
        view = self.service.get_case(case.case_id, "admin1")
        self.assertEqual(view["view"], "admin")
        submitters = {e["submitted_by"] for e in view["evidence"]}
        self.assertEqual(submitters, {"parent1", "owner1"})
        kinds = {t["kind"] for t in view["timeline"]}
        self.assertIn("filed", kinds)
        self.assertIn("evidence_added", kinds)
        self.assertTrue(any(r["officer_id"] == "rev_orig" for r in view["recusal_relations"]))

    def test_reviewer_lists_only_assigned_cases(self) -> None:
        c1, _ = self.file_case(idempotency_key="c1")
        c2, _ = self.file_case(idempotency_key="c2")
        self.service.assign_officers(c1.case_id, "admin1", ["rev_a"])
        listed = {c["case_id"] for c in self.service.list_cases("rev_a")}
        self.assertEqual(listed, {c1.case_id})


class DecisionDiffTest(AppealServiceTestBase):
    def test_diff_between_old_and_new_decision(self) -> None:
        case, _ = self.file_case()
        self.advance_to_review(case.case_id)
        self.issue_first_decision(holding="不成立", body="维持原使用安排\n驳回申诉")
        self.service.reopen_case(case.case_id, "parent1", "新证据")
        self.service.begin_review(case.case_id, "rev_a")
        self.service.issue_decision(
            case.case_id, "rev_a", holding="成立", body="停止使用作品\n赔偿署名损失",
            signer_ids=["rev_a", "rev_b", "rev_c"],
        )
        report = self.service.decision_history_diff(case.case_id, "admin1")
        diff = report["diffs"][0]
        self.assertTrue(diff["holding_changed"])
        self.assertEqual(diff["holding"], {"old": "不成立", "new": "成立"})
        self.assertTrue(any("停止使用作品" in line for line in diff["body_unified_diff"]))

    def test_diff_requires_admin(self) -> None:
        case, _ = self.file_case()
        with self.assertRaises(AuthorizationError):
            self.service.decision_history_diff(case.case_id, "parent1")


class SeedTest(unittest.TestCase):
    def test_seed_demo_builds_scenario(self) -> None:
        service, _repo, _contract = build_app()
        ids = seed_demo(service)
        view = service.get_case(ids["case_id"], "admin1")
        self.assertEqual(len(view["evidence"]), 2)
        self.assertEqual(view["evidence"][0]["note"], "最初版本")


class ContractBackedConfigTest(unittest.TestCase):
    def test_policies_come_from_contract(self) -> None:
        contract = load_contract(ROOT / "domain" / "contract.json")
        service = create_appeal_service(contract, repo=Repository())
        import appeals.policy as policy

        self.assertEqual(policy.ACCEPTANCE_DAYS, contract["policies"]["acceptance_days"])
        self.assertEqual(policy.REQUIRED_SIGNATURES, contract["policies"]["required_signatures"])


if __name__ == "__main__":
    unittest.main()
