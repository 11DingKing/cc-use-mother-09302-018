"""HTTP API 端到端测试（真实 socket + 后台线程）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from appeals.app import build_app
from appeals.http_api import AppealHTTPHandler


class TestHTTPClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HTTPApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        service, _repo, _contract = build_app()
        handler = type("BoundHandler", (AppealHTTPHandler,), {"service": service})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server.verbose = False
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = TestHTTPClient(f"http://127.0.0.1:{cls.port}")
        for uid, name, role in [
            ("parent1", "家长", "学生家长"),
            ("owner1", "负责人", "活动负责人"),
            ("rev_orig", "原评审", "学校复核人员"),
            ("rev_a", "复核A", "学校复核人员"),
            ("rev_b", "复核B", "学校复核人员"),
            ("rev_c", "复核C", "学校复核人员"),
            ("admin1", "管理员", "申诉办公室管理员"),
        ]:
            status, _ = cls.api.request("POST", "/users", {"user_id": uid, "name": name, "role": role})
            assert status == 201

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_01_full_scenario(self) -> None:
        api = self.api
        # 缺少 X-User-Id
        status, err = api.request("POST", "/cases", {"title": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "appeal_error")

        body = {
            "record_id": "WORK-1",
            "title": "作品使用异议",
            "grounds": ["未授权使用"],
            "other_party_ids": ["owner1"],
            "evidence": [{"filename": "p.jpg", "content": "v1", "channel": "online"}],
        }
        # 立案 + 幂等重试
        s1, c1 = api.request("POST", "/cases", body, {"X-User-Id": "parent1", "Idempotency-Key": "net-1"})
        s2, c2 = api.request("POST", "/cases", body, {"X-User-Id": "parent1", "Idempotency-Key": "net-1"})
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(c1["case_id"], c2["case_id"])
        self.assertTrue(c1["created"])
        self.assertFalse(c2["created"])
        case_id = c1["case_id"]

        # 同键不同体 -> 409
        s3, err3 = api.request(
            "POST", "/cases", {**body, "title": "篡改"}, {"X-User-Id": "parent1", "Idempotency-Key": "net-1"}
        )
        self.assertEqual(s3, 409)
        self.assertEqual(err3["error"], "idempotency_payload_mismatch")

        # 邮件补证追加 v2
        s4, ev = api.request(
            "POST", f"/cases/{case_id}/evidence",
            {"filename": "p.jpg", "content": "v2", "channel": "email", "supersedes": 1},
            {"X-User-Id": "parent1"},
        )
        self.assertEqual(s4, 201)
        self.assertEqual(ev["version"], 2)

        # 原评审登记回避，受理/复核被拒
        api.request("POST", "/recusals", {"officer_id": "rev_orig", "record_id": "WORK-1", "reason": "原评审人"}, {"X-User-Id": "admin1"})
        s5, err5 = api.request("POST", f"/cases/{case_id}/accept", {}, {"X-User-Id": "rev_orig"})
        self.assertEqual(s5, 422)
        self.assertEqual(err5["error"], "recusal_conflict")

        # 正常流程
        self.assertEqual(api.request("POST", f"/cases/{case_id}/accept", {}, {"X-User-Id": "admin1"})[0], 200)
        self.assertEqual(
            api.request("POST", f"/cases/{case_id}/assign", {"officer_ids": ["rev_a", "rev_b", "rev_c"]}, {"X-User-Id": "admin1"})[0],
            200,
        )
        self.assertEqual(api.request("POST", f"/cases/{case_id}/investigate", {}, {"X-User-Id": "rev_a"})[0], 200)
        self.assertEqual(api.request("POST", f"/cases/{case_id}/review", {}, {"X-User-Id": "rev_a"})[0], 200)

        # 签署人数不足
        s6, err6 = api.request(
            "POST", f"/cases/{case_id}/decisions",
            {"holding": "不成立", "body": "x", "signer_ids": ["rev_a"]},
            {"X-User-Id": "rev_a"},
        )
        self.assertEqual(s6, 422)
        self.assertEqual(err6["error"], "signature_error")

        # 合法决定
        s7, d1 = api.request(
            "POST", f"/cases/{case_id}/decisions",
            {"holding": "不成立", "body": "维持", "signer_ids": ["rev_a", "rev_b", "rev_c"]},
            {"X-User-Id": "rev_a"},
        )
        self.assertEqual(s7, 201)

        # 当事人隔离：家长看不到负责人的材料（本案无负责人材料，仅看到自己 2 份）
        s8, party_view = api.request("GET", f"/cases/{case_id}", None, {"X-User-Id": "parent1"})
        self.assertEqual(s8, 200)
        self.assertEqual({e["submitted_by"] for e in party_view["evidence"]}, {"parent1"})

        # 管理员看到完整时间线
        s9, admin_view = api.request("GET", f"/cases/{case_id}", None, {"X-User-Id": "admin1"})
        self.assertEqual(s9, 200)
        self.assertEqual(len(admin_view["evidence"]), 2)
        self.assertGreaterEqual(len(admin_view["timeline"]), 7)

        # 重开 -> 再复核 -> 新决定 -> 差异
        self.assertEqual(
            api.request("POST", f"/cases/{case_id}/reopen", {"reason": "新证据"}, {"X-User-Id": "parent1"})[0], 200
        )
        self.assertEqual(api.request("POST", f"/cases/{case_id}/review", {}, {"X-User-Id": "rev_a"})[0], 200)
        self.assertEqual(
            api.request(
                "POST", f"/cases/{case_id}/decisions",
                {"holding": "成立", "body": "改判", "signer_ids": ["rev_a", "rev_b", "rev_c"]},
                {"X-User-Id": "rev_a"},
            )[0],
            201,
        )
        s10, diff = api.request("GET", f"/cases/{case_id}/decision-diff", None, {"X-User-Id": "admin1"})
        self.assertEqual(s10, 200)
        self.assertTrue(diff["diffs"][0]["holding_changed"])

        # 非管理员看差异 -> 403
        s11, err11 = api.request("GET", f"/cases/{case_id}/decision-diff", None, {"X-User-Id": "parent1"})
        self.assertEqual(s11, 403)

    def test_02_other_party_forbidden(self) -> None:
        api = self.api
        status, _ = api.request(
            "POST", "/cases",
            {"record_id": "WORK-2", "title": "t", "grounds": ["g"]},
            {"X-User-Id": "owner1", "Idempotency-Key": "net-2"},
        )
        self.assertEqual(status, 201)
        listed_parent = api.request("GET", "/cases", None, {"X-User-Id": "parent1"})[1]
        parent_ids = {c["case_id"] for c in listed_parent}
        owner_cases = api.request("GET", "/cases", None, {"X-User-Id": "owner1"})[1]
        owner_id = next(c["case_id"] for c in owner_cases if c["record_id"] == "WORK-2")
        self.assertNotIn(owner_id, parent_ids)
        status, err = api.request("GET", f"/cases/{owner_id}", None, {"X-User-Id": "parent1"})
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "forbidden")


if __name__ == "__main__":
    unittest.main()
