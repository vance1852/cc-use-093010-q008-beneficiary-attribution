"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse, parse_qs

from .errors import ServiceError, ValidationFailed
from .service import AttributionService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到归属服务，便于无网络单元测试。"""

    def __init__(self, service: AttributionService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        actor = lambda: self._actor(normalized_headers)  # noqa: E731
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            service = self.service

            if method == "POST" and path == "/users":
                result = service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)
            if method == "POST" and path == "/policies":
                return Response(201, service.publish_policy(actor(), payload["policy"]))
            if method == "POST" and path == "/applicants":
                result = service.register_applicant(
                    actor(), payload["applicant_id"], payload["name"], payload["applicant_kind"],
                    payload["region_code"], payload.get("identities", []), payload.get("weak"),
                )
                return Response(201, result)
            if method == "POST" and path == "/beneficiaries":
                result = service.register_beneficiary(
                    actor(), payload["beneficiary_id"], payload["name"],
                    payload["beneficiary_kind"], payload["region_code"],
                )
                return Response(201, result)
            if method == "POST" and path == "/organizations":
                result = service.register_organization(
                    actor(), payload["organization_id"], payload["name"],
                    payload["organization_kind"], payload["region_code"],
                )
                return Response(201, result)
            if method == "POST" and path == "/relations":
                result = service.register_relation(
                    actor(), payload["relation_id"], payload["left_applicant_id"],
                    payload["right_applicant_id"], payload["kind"], payload["valid_from"],
                    organization_id=payload.get("organization_id"),
                    valid_to=payload.get("valid_to"),
                    supersedes_relation_id=payload.get("supersedes_relation_id"),
                )
                return Response(201, result)
            if method == "POST" and path == "/resources":
                result = service.register_resource(
                    actor(), payload["resource_id"], payload["program_id"],
                    payload["resource_type"], payload["region_code"], payload.get("detail"),
                )
                return Response(201, result)
            if method == "POST" and path == "/claims":
                result = service.register_claim(
                    actor(), payload["claim_id"], payload["applicant_id"],
                    payload["beneficiary_id"], payload["program_id"], payload["region_code"],
                    resource_id=payload.get("resource_id"), chain=payload.get("chain", []),
                )
                return Response(201, result)
            if method == "POST" and path == "/milestones":
                result = service.register_milestone(
                    actor(), payload["milestone_id"], payload["evidence_type"],
                    int(payload["evidence_level"]), payload["occurred_at"],
                    payload["content_sha256"], claim_id=payload.get("claim_id"),
                    late=bool(payload.get("late", False)),
                )
                return Response(201, result)
            if method == "POST" and path == "/outcomes":
                return Response(201, service.register_outcome(actor(), payload))
            if method == "POST" and path == "/callbacks":
                result = service.ingest_callback(
                    actor(), payload["callback_id"], payload["source"], payload["payload"]
                )
                return Response(200 if result["status"] == "duplicate" else 201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "duplicates" and parts[2] == "detect":
                as_of = query.get("as_of", [None])[0]
                result = service.detect_duplicates(
                    actor(), parts[1], int(payload["version"]) if payload.get("version") is not None else None,
                    as_of=as_of,
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "outcomes" and parts[2] == "attribute":
                result = service.run_attribution(
                    actor(), parts[1], payload["policy_id"],
                    version=int(payload["version"]) if payload.get("version") is not None else None,
                    reason=payload.get("reason", ""), declared_shares=payload.get("declared_shares"),
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "attributions":
                return Response(200, service.get_attribution(actor(), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "attributions" and parts[2] == "publish":
                result = service.publish_attribution(
                    actor(), parts[1], payload["channel"], payload["receipt_sha256"]
                )
                return Response(200, result)
            if method == "POST" and path == "/disputes":
                result = service.raise_dispute(actor(), payload["attribution_id"], payload["reason"])
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "review":
                result = service.start_review(actor(), int(parts[1]))
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "complete":
                result = service.complete_review(
                    actor(), int(parts[1]), payload["decision"], payload["finding"],
                    policy_id=payload.get("policy_id"),
                    version=int(payload["version"]) if payload.get("version") is not None else None,
                    declared_shares=payload.get("declared_shares"),
                )
                return Response(200, result)
            if method == "GET" and path == "/disputes":
                status = query.get("status", [None])[0]
                return Response(200, service.list_disputes(actor(), status))
            if method == "GET" and len(parts) == 3 and parts[0] == "reports" and parts[1] == "coverage":
                version = int(query["version"][0]) if query.get("version") else None
                return Response(200, service.coverage_report(actor(), parts[2], version))
            if method == "GET" and len(parts) == 3 and parts[0] == "outcomes" and parts[2] == "lineage":
                return Response(200, service.outcome_lineage(actor(), parts[1]))
            if method == "GET" and path == "/claim_views":
                program_id = query.get("program_id", [None])[0]
                return Response(200, service.claim_views(actor(), program_id))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    # 服务共享单个 SQLite 连接；用锁把请求串行化，
    # 避免工作线程在同一连接上交叉开启事务。
    dispatch_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "BenefitAttribution/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with dispatch_lock:
                response = application.handle(
                    self.command, self.path, dict(self.headers.items()), body
                )
                encoded = json.dumps(
                    response.body, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动受益关系与成效归属 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("benefit-attribution.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    application = JsonApplication(AttributionService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
