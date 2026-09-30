"""申诉后端包。"""
from .clock import Clock, MovableClock, system_clock
from .errors import AppealError
from .service import AppealService
from .states import configure_from_contract as configure_states
from .policy import configure_from_contract as configure_policy
from .storage import Repository


def create_appeal_service(contract: dict, repo: Repository | None = None, clock: Clock | None = None) -> AppealService:
    """根据领域契约装配业务服务（状态机与策略参数来自契约）。"""
    configure_states(contract["transitions"])
    configure_policy(contract["policies"])
    return AppealService(repo or Repository(), clock or system_clock())
