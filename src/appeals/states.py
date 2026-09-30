"""案件状态推进规则（与 domain/contract.json 的 transitions 对应）。"""
from __future__ import annotations

from .errors import IllegalTransitionError

# 默认推进表；服务启动时会用契约中的 transitions 覆盖。
TRANSITIONS: dict[str, list[str]] = {
    "提出": ["受理", "已撤回"],
    "受理": ["调查", "已撤回"],
    "调查": ["复核", "已撤回"],
    "复核": ["决定", "已撤回"],
    "决定": ["重开"],
    "重开": ["复核"],
    # 终态
    "已撤回": [],
    "已合并": [],
}


def configure_from_contract(transitions: dict[str, list[str]]) -> None:
    """用领域契约中的推进表替换默认值。"""
    global TRANSITIONS
    TRANSITIONS = {state: list(targets) for state, targets in transitions.items()}
    for terminal in ("已撤回", "已合并"):
        TRANSITIONS.setdefault(terminal, [])


def ensure_transition(current: str, target: str) -> None:
    allowed = TRANSITIONS.get(current, [])
    if target not in allowed:
        raise IllegalTransitionError(f"案件不能从「{current}」推进到「{target}」，允许的状态：{('、'.join(allowed)) or '无'}")


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, [])


# 决定作出前的状态（可撤回、可合并、可补证）
PRE_DECISION_STATES = ("提出", "受理", "调查", "复核")
# 可申请临时措施的状态
INTERIM_MEASURE_STATES = ("受理", "调查", "复核", "重开")
