"""申诉领域错误类型。"""
from __future__ import annotations


class AppealError(Exception):
    """所有领域错误的基类。"""

    code = "appeal_error"
    http_status = 400


class NotFoundError(AppealError):
    code = "not_found"
    http_status = 404


class ValidationError(AppealError):
    code = "validation_error"
    http_status = 400


class AuthorizationError(AppealError):
    code = "forbidden"
    http_status = 403


class IllegalTransitionError(AppealError):
    code = "illegal_transition"
    http_status = 409


class DeadlineError(AppealError):
    code = "deadline_violation"
    http_status = 422


class RecusalConflictError(AppealError):
    code = "recusal_conflict"
    http_status = 422


class SignatureError(AppealError):
    code = "signature_error"
    http_status = 422


class IdempotencyMismatchError(AppealError):
    code = "idempotency_payload_mismatch"
    http_status = 409


class MergeError(AppealError):
    code = "merge_error"
    http_status = 422
