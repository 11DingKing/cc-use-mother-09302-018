"""零依赖 HTTP API（标准库 http.server）。

鉴权：所有业务请求须带 X-User-Id 头，标识操作人；管理员接口由服务层角色校验兜底。
幂等：立案接口读取 Idempotency-Key 请求头，重试不产生重复案件。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import build_app
from .errors import AppealError

_CASE_ID = r"(?P<case_id>[0-9A-Za-z\-]+)"
_MEASURE_ID = r"(?P<measure_id>[0-9A-Za-z\-]+)"

ROUTES = [
    ("POST", re.compile(r"^/users$"), "register_user"),
    ("POST", re.compile(r"^/recusals$"), "add_recusal"),
    ("POST", re.compile(r"^/cases/merge$"), "merge_cases"),
    ("POST", re.compile(r"^/cases$"), "file_appeal"),
    ("GET", re.compile(r"^/cases$"), "list_cases"),
    ("GET", re.compile(rf"^/cases/{_CASE_ID}$"), "get_case"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/evidence$"), "submit_evidence"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/accept$"), "accept"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/assign$"), "assign"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/investigate$"), "investigate"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/review$"), "review"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/decisions$"), "issue_decision"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/withdraw$"), "withdraw"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/reopen$"), "reopen"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/interim-measures$"), "interim_request"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/interim-measures/{_MEASURE_ID}/decide$"), "interim_decide"),
    ("POST", re.compile(rf"^/cases/{_CASE_ID}/interim-measures/{_MEASURE_ID}/lift$"), "interim_lift"),
    ("GET", re.compile(rf"^/cases/{_CASE_ID}/decision-diff$"), "decision_diff"),
    ("GET", re.compile(r"^/records/(?P<record_id>[0-9A-Za-z\-]+)/recusals$"), "recusal_check"),
]


def _serialize(result) -> dict | list:
    if isinstance(result, tuple):
        obj, created = result
        if hasattr(obj, "to_detail_dict"):
            payload = obj.to_detail_dict()
            payload["created"] = created
            return payload
    if hasattr(result, "to_detail_dict"):
        return result.to_detail_dict()
    if hasattr(result, "to_dict"):
        return result.to_dict()
    return result


class AppealHTTPHandler(BaseHTTPRequestHandler):
    service = None

    # silence default noisy logging; keep a compact access line
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AppealError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise AppealError("请求体必须是 JSON 对象")
        return value

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> str:
        actor = self.headers.get("X-User-Id", "").strip()
        if not actor:
            raise AppealError("缺少 X-User-Id 请求头")
        return actor

    def _dispatch(self, method: str):
        try:
            body = self._read_json() if method == "POST" else {}
            for verb, pattern, action in ROUTES:
                if verb != method:
                    continue
                match = pattern.match(self.path.split("?")[0])
                if match:
                    status, payload = getattr(self, f"do_{action}")(match.groupdict(), body)
                    self._send(status, _serialize(payload))
                    return
            self._send(404, {"error": "not_found", "message": f"无此路由：{method} {self.path}"})
        except AppealError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接挂起
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    # ---------------- 具体动作 ----------------
    def do_register_user(self, params: dict, body: dict):
        user = self.service.register_user(body["user_id"], body["name"], body["role"])
        return 201, user

    def do_add_recusal(self, params: dict, body: dict):
        rel = self.service.add_recusal_relation(body["officer_id"], body["record_id"], body["reason"])
        return 201, rel

    def do_file_appeal(self, params: dict, body: dict):
        key = self.headers.get("Idempotency-Key", "").strip() or None
        result = self.service.file_appeal(
            appellant_user_id=self._actor(),
            record_id=body.get("record_id", ""),
            title=body.get("title", ""),
            grounds=body.get("grounds", []),
            other_party_ids=body.get("other_party_ids", []),
            idempotency_key=key,
            initial_evidence=body.get("evidence", []),
        )
        return 201, result

    def do_list_cases(self, params: dict, body: dict):
        return 200, self.service.list_cases(self._actor())

    def do_get_case(self, params: dict, body: dict):
        return 200, self.service.get_case(params["case_id"], self._actor())

    def do_submit_evidence(self, params: dict, body: dict):
        return 201, self.service.submit_evidence(params["case_id"], self._actor(), body)

    def do_accept(self, params: dict, body: dict):
        return 200, self.service.accept_appeal(params["case_id"], self._actor(), body.get("note", ""))

    def do_assign(self, params: dict, body: dict):
        return 200, self.service.assign_officers(params["case_id"], self._actor(), body.get("officer_ids", []))

    def do_investigate(self, params: dict, body: dict):
        return 200, self.service.begin_investigation(params["case_id"], self._actor())

    def do_review(self, params: dict, body: dict):
        return 200, self.service.begin_review(params["case_id"], self._actor())

    def do_issue_decision(self, params: dict, body: dict):
        decision = self.service.issue_decision(
            params["case_id"],
            self._actor(),
            holding=body["holding"],
            body=body["body"],
            signer_ids=body["signer_ids"],
        )
        return 201, decision

    def do_withdraw(self, params: dict, body: dict):
        return 200, self.service.withdraw_appeal(params["case_id"], self._actor(), body.get("reason", ""))

    def do_reopen(self, params: dict, body: dict):
        return 200, self.service.reopen_case(params["case_id"], self._actor(), body.get("reason", ""))

    def do_merge_cases(self, params: dict, body: dict):
        return 200, self.service.merge_cases(
            self._actor(), body["lead_case_id"], body.get("subordinate_case_ids", [])
        )

    def do_interim_request(self, params: dict, body: dict):
        return 201, self.service.request_interim_measure(
            params["case_id"], self._actor(), body.get("content", "")
        )

    def do_interim_decide(self, params: dict, body: dict):
        return 200, self.service.decide_interim_measure(
            params["case_id"],
            self._actor(),
            params["measure_id"],
            bool(body.get("grant", False)),
            body.get("remark", ""),
        )

    def do_interim_lift(self, params: dict, body: dict):
        return 200, self.service.lift_interim_measure(
            params["case_id"], self._actor(), params["measure_id"]
        )

    def do_decision_diff(self, params: dict, body: dict):
        return 200, self.service.decision_history_diff(params["case_id"], self._actor())

    def do_recusal_check(self, params: dict, body: dict):
        return 200, self.service.recusal_check(self._actor(), params["record_id"])


def create_server(host: str = "127.0.0.1", port: int = 8080, *, seed: bool = False, verbose: bool = False):
    service, _repo, _contract = build_app()
    if seed:
        from .app import seed_demo

        seed_demo(service)

    class _Handler(AppealHTTPHandler):
        pass

    _Handler.service = service
    server = ThreadingHTTPServer((host, port), _Handler)
    server.verbose = verbose
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="校园文化活动申诉后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="写入演示数据")
    args = parser.parse_args()
    server = create_server(args.host, args.port, seed=args.seed, verbose=True)
    print(f"申诉后端已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
