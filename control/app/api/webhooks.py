"""Webhook endpoints cho Linear và GitHub (POST /api/v1/webhooks/linear, /github).

Giai đoạn S4: Kiểm tra chữ ký bảo mật và ghi nhận sự kiện hệ thống, chưa hành động nghiệp vụ.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode
from agent_contract.models import AgentError
from control.app.api.dependencies import get_db, get_publisher, get_redis
from control.app.queue.events import EventPublisher
from control.app.queue.redis import IdempotencyManager

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def verify_hmac_sha256(raw_body: bytes, secret: str, signature_header: Optional[str], prefix: str = "") -> bool:
    """Xác minh chữ ký HMAC-SHA256 của Webhook."""
    if not secret:
        # Trong môi trường dev/test khi chưa cấu hình webhook secret, cho phép bỏ qua nếu không gửi header
        return True

    if not signature_header:
        return False

    expected_sig = signature_header.strip()
    if prefix and expected_sig.startswith(prefix):
        expected_sig = expected_sig[len(prefix):]

    computed_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_sig, expected_sig)


@router.post("/linear")
async def linear_webhook(
    request: Request,
    linear_signature: Optional[str] = Header(None, alias="Linear-Signature"),
    linear_delivery: Optional[str] = Header(None, alias="Linear-Delivery"),
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    redis_client=Depends(get_redis),
) -> Dict[str, Any]:
    """Tiếp nhận Webhook từ Linear kèm xác thực chữ ký HMAC-SHA256."""
    raw_body = await request.body()
    webhook_secret = os.getenv("LINEAR_WEBHOOK_SECRET", "")

    # Xác thực chữ ký nếu có cấu hình secret hoặc có gửi chữ ký
    if webhook_secret or linear_signature:
        if not verify_hmac_sha256(raw_body, webhook_secret, linear_signature):
            raise HTTPException(
                status_code=401,
                detail=AgentError(
                    code=ErrorCode.PERMISSION_DENIED,
                    message="Chữ ký Webhook Linear không hợp lệ.",
                    retryable=False,
                ).model_dump(),
            )

    # Chống trùng lặp (Idempotency) nếu có Delivery ID
    if linear_delivery:
        idempotency = IdempotencyManager(redis_client)
        if not idempotency.check_and_claim(f"linear:{linear_delivery}", ttl_seconds=86400):
            return {"status": "ignored", "reason": "duplicate_delivery"}

    try:
        payload = await request.json()
    except Exception:
        payload = {"raw_size": len(raw_body)}

    # Ghi nhận sự kiện vào Redis Stream và MySQL
    msg_id = publisher.publish(
        event_type="LINEAR_WEBHOOK_RECEIVED",
        payload={"action": payload.get("action"), "type": payload.get("type"), "data": payload.get("data")},
        session=session,
    )
    session.commit()

    return {"status": "received", "event_id": msg_id}


@router.post("/github")
async def github_webhook(
    request: Request,
    hub_signature: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    github_delivery: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    redis_client=Depends(get_redis),
) -> Dict[str, Any]:
    """Tiếp nhận Webhook từ GitHub kèm xác thực chữ ký HMAC-SHA256."""
    raw_body = await request.body()
    webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")

    # Xác minh chữ ký header sha256=<hash>
    if webhook_secret or hub_signature:
        if not verify_hmac_sha256(raw_body, webhook_secret, hub_signature, prefix="sha256="):
            raise HTTPException(
                status_code=401,
                detail=AgentError(
                    code=ErrorCode.PERMISSION_DENIED,
                    message="Chữ ký Webhook GitHub không hợp lệ.",
                    retryable=False,
                ).model_dump(),
            )

    # Chống trùng lặp
    if github_delivery:
        idempotency = IdempotencyManager(redis_client)
        if not idempotency.check_and_claim(f"github:{github_delivery}", ttl_seconds=86400):
            return {"status": "ignored", "reason": "duplicate_delivery"}

    try:
        payload = await request.json()
    except Exception:
        payload = {"raw_size": len(raw_body)}

    # Ghi nhận sự kiện
    msg_id = publisher.publish(
        event_type="GITHUB_WEBHOOK_RECEIVED",
        payload={"action": payload.get("action"), "repository": payload.get("repository", {}).get("full_name")},
        session=session,
    )
    session.commit()

    return {"status": "received", "event_id": msg_id}
