"""期限与法定签署策略。默认值与 domain/contract.json 的 policies 一致。"""
from __future__ import annotations

ACCEPTANCE_DAYS = 5      # 受理审查期限
REOPEN_DAYS = 15         # 决定后可申请重开的期限
REQUIRED_SIGNATURES = 3  # 作出决定的法定签署人数


def configure_from_contract(policies: dict) -> None:
    global ACCEPTANCE_DAYS, REOPEN_DAYS, REQUIRED_SIGNATURES
    ACCEPTANCE_DAYS = int(policies["acceptance_days"])
    REOPEN_DAYS = int(policies["reopen_days"])
    REQUIRED_SIGNATURES = int(policies["required_signatures"])
