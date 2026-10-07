"""受益关系与成效归属的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import POLICY_VERSION_LATEST, AttributionService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

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
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)  # noqa: E731

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)
            if method == "POST" and path == "/policies":
                return Response(201, self.service.publish_policy(actor(), payload))
            if method == "GET" and path == "/policies":
                return Response(200, {"policies": self.service.list_policies()})

            if method == "POST" and path == "/applicants":
                return Response(201, self.service.register_applicant(
                    actor(), payload["applicant_id"], payload["name"], payload["applicant_kind"],
                    payload["region_code"], channel=payload["channel"],
                    legal_identity_code=payload.get("legal_identity_code"),
                    contact_fingerprint=payload.get("contact_fingerprint"), detail=payload.get("detail"),
                ))
            if method == "POST" and path == "/beneficiaries":
                return Response(201, self.service.register_beneficiary(
                    actor(), payload["beneficiary_id"], payload["name"], payload["beneficiary_kind"],
                    payload["region_code"], legal_identity_code=payload.get("legal_identity_code"),
                    detail=payload.get("detail"),
                ))
            if method == "POST" and path == "/org_relations":
                return Response(201, self.service.register_org_relation(
                    actor(), payload["relation_id"], payload["relation_kind"],
                    payload["left_org_id"], payload["right_org_id"], payload["valid_from"],
                    valid_to=payload.get("valid_to"),
                    supersedes_relation_id=payload.get("supersedes_relation_id"),
                    detail=payload.get("detail"),
                ))
            if method == "POST" and path == "/resources":
                return Response(201, self.service.register_resource(
                    actor(), payload["resource_id"], payload["resource_kind"], payload["program_id"],
                    payload["region_code"], batch_ref=payload.get("batch_ref"),
                    detail=payload.get("detail"),
                ))
            if method == "POST" and path == "/evidence":
                return Response(201, self.service.register_evidence(
                    actor(), payload["evidence_id"], payload["milestone_kind"], payload["occurred_at"],
                    payload["reporting_org"], detail=payload.get("detail"),
                ))
            if method == "POST" and path == "/claims":
                return Response(201, self.service.register_claim(
                    actor(), payload["claim_id"], payload["applicant_id"], payload["beneficiary_id"],
                    payload["resource_id"], payload["milestone_kind"], payload["outcome_value"],
                    payload["evidence_id"], payload["reported_by_org"],
                    attributes=payload.get("attributes"),
                ))
            if method == "POST" and path == "/callbacks":
                key = normalized_headers.get("idempotency-key", "").strip()
                if not key:
                    raise ValidationFailed("缺少 Idempotency-Key")
                scope = normalized_headers.get("callback-scope", "default").strip() or "default"
                return Response(200, self.service.ingest_callback(actor(), scope, key, payload))

            if method == "POST" and path == "/attributions":
                result = self.service.run_attribution(
                    actor(), payload["attribution_id"], payload["policy_id"], payload["claim_ids"],
                    policy_version=payload.get("policy_version", POLICY_VERSION_LATEST),
                    as_of=payload.get("as_of"), change_reason=payload.get("change_reason"),
                )
                return Response(201 if not result.get("replayed") else 200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "attributions" and parts[2] == "publish":
                result = self.service.publish_attribution(actor(), parts[1], int(payload["version_no"]))
                return Response(200, result)
            if method == "POST" and path == "/disputes":
                result = self.service.raise_dispute(
                    actor(), payload["attribution_id"], int(payload["version_no"]), payload["reason"],
                    cluster_key=payload.get("cluster_key"),
                )
                return Response(201, result)
            if method == "GET" and path == "/disputes":
                return Response(200, {"disputes": self.service.open_disputes(actor())})
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "review":
                return Response(200, self.service.start_review(actor(), int(parts[1])))
            if method == "POST" and len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return Response(200, self.service.resolve_dispute(
                    actor(), int(parts[1]), bool(payload["uphold"]), payload["determination_note"],
                    policy_id=payload.get("policy_id"), policy_version=payload.get("policy_version"),
                    claim_ids=payload.get("claim_ids"),
                ))

            if method == "GET" and len(parts) == 4 and parts[0] == "attributions" and parts[2] == "versions":
                return Response(200, {
                    "attribution_id": parts[1],
                    "version_no": int(parts[3]),
                    "chain": self.service.source_chain(actor(), parts[1], int(parts[3])),
                })
            if method == "GET" and len(parts) == 2 and parts[0] == "attributions":
                return Response(200, {
                    "attribution_id": parts[1],
                    "versions": self.service.list_versions(actor(), parts[1]),
                    "chain": self.service.source_chain(actor(), parts[1]),
                })
            if method == "GET" and len(parts) == 2 and parts[0] == "coverage":
                policy_version = query.get("policy_version", [POLICY_VERSION_LATEST])[0]
                policy_version = POLICY_VERSION_LATEST if policy_version == "latest" else int(policy_version)
                return Response(200, self.service.coverage(
                    actor(), parts[1], policy_version,
                    as_of=query.get("as_of", [None])[0],
                    region_code=query.get("region", [None])[0],
                ))
            if method == "GET" and len(parts) == 2 and parts[0] == "dashboard":
                policy_version = query.get("policy_version", [POLICY_VERSION_LATEST])[0]
                policy_version = POLICY_VERSION_LATEST if policy_version == "latest" else int(policy_version)
                return Response(200, self.service.dashboard(
                    actor(), parts[1], policy_version, as_of=query.get("as_of", [None])[0],
                ))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BenefitAttribution/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
    parser.add_argument("--database", type=Path, default=Path("benefit_attribution.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
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
