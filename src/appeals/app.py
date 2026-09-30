"""应用装配：加载契约、创建服务、可选种子数据。"""
from __future__ import annotations

from pathlib import Path

from domain_contract.validator import load_contract

from .clock import MovableClock
from .service import AppealService
from .storage import Repository
from . import create_appeal_service

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "domain" / "contract.json"


def build_app(clock=None) -> tuple[AppealService, Repository, dict]:
    contract = load_contract(CONTRACT_PATH)
    repo = Repository()
    service = create_appeal_service(contract, repo=repo, clock=clock or MovableClock())
    return service, repo, contract


def seed_demo(service: AppealService) -> dict:
    """构造覆盖典型场景的演示数据，返回关键编号。"""
    service.register_user("parent1", "李家长", "学生家长")
    service.register_user("parent2", "王家长", "学生家长")
    service.register_user("owner1", "周指导", "活动负责人")
    service.register_user("rev_orig", "赵评审", "学校复核人员")
    service.register_user("rev_a", "钱复核", "学校复核人员")
    service.register_user("rev_b", "孙复核", "学校复核人员")
    service.register_user("rev_c", "李复核", "学校复核人员")
    service.register_user("admin1", "办公室管理员", "申诉办公室管理员")

    # 赵评审是原决定的评审人，对该作品记录须回避
    service.add_recusal_relation("rev_orig", "WORK-2026-018", "原作品使用决定评审人")

    case, _ = service.file_appeal(
        appellant_user_id="parent1",
        record_id="WORK-2026-018",
        title="对孩子作品被第三方使用的异议",
        grounds=["作品被商业展板使用未征得同意", "署名信息不完整"],
        other_party_ids=["owner1"],
        idempotency_key="seed-case-1",
        initial_evidence=[
            {"filename": "作品照片.jpg", "content": "v1-binary", "channel": "online", "note": "最初版本"},
        ],
    )
    # 邮件补证：追加 v2，不覆盖 v1
    service.submit_evidence(
        case.case_id,
        "parent1",
        {"filename": "作品照片.jpg", "content": "v2-email-binary", "channel": "email", "note": "邮件补充的高清版", "supersedes": 1},
    )
    return {"case_id": case.case_id}
